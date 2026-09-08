"""Central, durable and default-off Slack operations connector."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from hmac import compare_digest
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from slack_correlation.catalog import NotificationCommand
from slack_correlation.client import SlackClient, SlackProtocolError
from slack_correlation.store import NotificationCapacityError, NotificationStore
from slack_correlation.worker import NotificationWorker, SlackMessageSender

_TEAM_ID = re.compile(r"^T[A-Z0-9]{8,}$")
_CHANNEL_ID = re.compile(r"^C[A-Z0-9]{8,}$")
_INTERNAL_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_WORKER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_SLACK_TS = re.compile(r"^[0-9]{10,16}\.[0-9]{6}$")
_ALLOWED_TENANTS = {"johanna", "att1"}
_MAX_BODY_BYTES = 8192


class PayloadTooLarge(ValueError):
    pass


class SlackConnectorClient(SlackMessageSender, Protocol):
    async def verify_auth(self, *, expected_team_id: str) -> None: ...


@dataclass(frozen=True)
class SlackConnectorSettings:
    """Explicit runtime state for the central Slack connector."""

    ingress_enabled: bool = False
    notifications_enabled: bool = False
    interactions_enabled: bool = False
    connectivity_check_enabled: bool = False
    bot_token: str | None = None
    signing_secret: str | None = None
    team_id: str | None = None
    channel_id: str | None = None
    storage_path: str = "/app/data/slack-connector.sqlite3"
    tenant_tokens: dict[str, str] = field(default_factory=dict)
    worker_id: str = "slack-worker-1"
    poll_interval_seconds: float = 1.0
    max_nonterminal_notifications: int = 10_000
    activation_mode: str = "inactive"
    activation_generation: int = 0
    operator_bearer_token: str | None = None
    storage_preflight_enabled: bool = False

    @classmethod
    def from_env(cls) -> "SlackConnectorSettings":
        return cls(
            ingress_enabled=_env_flag("SLACK_INGRESS_ENABLED"),
            notifications_enabled=_env_flag("SLACK_NOTIFICATIONS_ENABLED"),
            interactions_enabled=_env_flag("SLACK_INTERACTIONS_ENABLED"),
            connectivity_check_enabled=_env_flag(
                "SLACK_CONNECTIVITY_CHECK_ENABLED"
            ),
            bot_token=_env_value("SLACK_BOT_TOKEN"),
            signing_secret=_env_value("SLACK_SIGNING_SECRET"),
            team_id=_env_value("SLACK_TEAM_ID"),
            channel_id=_env_value("SLACK_CHANNEL_ID"),
            storage_path=os.getenv(
                "SLACK_STORAGE_PATH", "/app/data/slack-connector.sqlite3"
            ).strip(),
            tenant_tokens=_env_tenant_tokens(),
            worker_id=os.getenv("SLACK_WORKER_ID", "slack-worker-1").strip(),
            poll_interval_seconds=_env_positive_float(
                "SLACK_POLL_INTERVAL_SECONDS", default=1.0
            ),
            max_nonterminal_notifications=_env_bounded_int(
                "SLACK_MAX_NONTERMINAL_NOTIFICATIONS",
                default=10_000,
                minimum=1,
                maximum=100_000,
            ),
            activation_mode=os.getenv("SLACK_ACTIVATION_MODE", "inactive").strip(),
            activation_generation=_env_bounded_int(
                "SLACK_ACTIVATION_GENERATION",
                default=0,
                minimum=0,
                maximum=2_147_483_647,
            ),
            operator_bearer_token=_env_value("SLACK_OPERATOR_BEARER_TOKEN"),
            storage_preflight_enabled=_env_flag("SLACK_STORAGE_PREFLIGHT_ENABLED"),
        )


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"invalid_boolean:{name}")
    return value == "true"


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _env_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"invalid_number:{name}") from exc
    if not 1.0 <= value <= 60:
        raise ValueError(f"invalid_number:{name}")
    return value


def _env_bounded_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid_integer:{name}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"invalid_integer:{name}")
    return value


def _env_tenant_tokens() -> dict[str, str]:
    raw = os.getenv("SLACK_TENANT_TOKENS_JSON")
    if raw is None or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("invalid_json:SLACK_TENANT_TOKENS_JSON") from exc
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in payload.items()
    ):
        raise ValueError("invalid_json:SLACK_TENANT_TOKENS_JSON")
    return dict(payload)


def _validate_settings(settings: SlackConnectorSettings) -> None:
    if settings.interactions_enabled:
        raise ValueError("interactions_not_implemented")
    if not 1.0 <= settings.poll_interval_seconds <= 60:
        raise ValueError("invalid_poll_interval")
    if not 1 <= settings.max_nonterminal_notifications <= 100_000:
        raise ValueError("invalid_notification_capacity")
    if _WORKER_ID.fullmatch(settings.worker_id) is None:
        raise ValueError("invalid_worker_id")
    if settings.activation_mode not in {"inactive", "one_shot", "continuous"}:
        raise ValueError("invalid_activation_mode")
    if settings.activation_generation < 0 or (
        settings.activation_mode != "inactive" and settings.activation_generation < 1
    ):
        raise ValueError("invalid_activation_generation")
    if settings.notifications_enabled and settings.activation_mode == "inactive":
        raise ValueError("explicit_activation_required")
    if settings.operator_bearer_token is not None and (
        _INTERNAL_TOKEN.fullmatch(settings.operator_bearer_token) is None
        or settings.operator_bearer_token in settings.tenant_tokens.values()
    ):
        raise ValueError("invalid_operator_token_configuration")
    if settings.ingress_enabled:
        if set(settings.tenant_tokens) != _ALLOWED_TENANTS:
            raise ValueError("tenant_token_configuration_incomplete")
        values = list(settings.tenant_tokens.values())
        if (
            any(_INTERNAL_TOKEN.fullmatch(value) is None for value in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("invalid_tenant_token_configuration")
        if not settings.storage_path:
            raise ValueError("storage_path_required")
    needs_slack = settings.connectivity_check_enabled or settings.notifications_enabled
    if needs_slack:
        bot_token = settings.bot_token
        team_id = settings.team_id
        channel_id = settings.channel_id
        if not all((bot_token, team_id, channel_id)):
            raise ValueError("connectivity_configuration_incomplete")
        assert bot_token is not None
        assert team_id is not None
        assert channel_id is not None
        if (
            not bot_token.startswith("xoxb-")
            or _TEAM_ID.fullmatch(team_id) is None
            or _CHANNEL_ID.fullmatch(channel_id) is None
        ):
            raise ValueError("invalid_slack_configuration")
    if settings.notifications_enabled and not settings.storage_path:
        raise ValueError("storage_path_required")
    if settings.operator_bearer_token is not None and (
        settings.channel_id is None or _CHANNEL_ID.fullmatch(settings.channel_id) is None
    ):
        raise ValueError("operator_channel_required")


def create_app(
    settings: SlackConnectorSettings,
    *,
    slack_client: SlackConnectorClient | None = None,
    store: NotificationStore | None = None,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Create the connector with independently gated ingress and outbound."""

    _validate_settings(settings)
    persistence = store or NotificationStore(Path(settings.storage_path))
    state = {
        "connectivity_verified": False,
        "instance_locked": False,
        "storage_ready": False,
        "worker_running": False,
        "last_connectivity_check": 0.0,
    }
    worker: NotificationWorker | None = None
    runtime_client = slack_client
    worker_start_lock = asyncio.Lock()

    async def ensure_worker_started() -> None:
        nonlocal worker
        if not settings.notifications_enabled:
            return
        async with worker_start_lock:
            if worker is not None:
                return
            if not state["storage_ready"] or not state["connectivity_verified"]:
                return
            assert runtime_client is not None
            assert settings.channel_id is not None
            candidate = NotificationWorker(
                store=persistence,
                slack_client=runtime_client,
                channel_id=settings.channel_id,
                tenant_labels={"johanna": "Johanna", "att1": "ATT1"},
                worker_id=settings.worker_id,
                poll_interval_seconds=settings.poll_interval_seconds,
            )
            await candidate.start()
            worker = candidate
            state["worker_running"] = True

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal runtime_client, worker
        requires_storage = (
            settings.storage_preflight_enabled
            or settings.ingress_enabled
            or settings.notifications_enabled
            or settings.operator_bearer_token is not None
        )
        if requires_storage:
            try:
                persistence.acquire_instance_lock()
                state["instance_locked"] = True
                await _to_thread(persistence.initialize)
                if settings.activation_mode != "inactive":
                    await _to_thread(
                        persistence.configure_activation,
                        mode=settings.activation_mode,
                        generation=settings.activation_generation,
                    )
                state["storage_ready"] = True
            except Exception:
                state["storage_ready"] = False

        needs_slack = settings.connectivity_check_enabled or settings.notifications_enabled
        if needs_slack and (not requires_storage or state["storage_ready"]):
            assert settings.bot_token is not None
            assert settings.team_id is not None
            if runtime_client is None:
                runtime_client = SlackClient(bot_token=settings.bot_token)
            try:
                await runtime_client.verify_auth(expected_team_id=settings.team_id)
            except SlackProtocolError:
                state["connectivity_verified"] = False
            else:
                state["connectivity_verified"] = True
            state["last_connectivity_check"] = monotonic_clock()

        await ensure_worker_started()
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
                state["worker_running"] = False
            if requires_storage:
                persistence.release_instance_lock()

    app = FastAPI(
        title="SupportMagician Slack Connector",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        requires_storage = (
            settings.storage_preflight_enabled
            or settings.ingress_enabled
            or settings.notifications_enabled
            or settings.operator_bearer_token is not None
        )
        needs_slack = settings.connectivity_check_enabled or settings.notifications_enabled
        if requires_storage and state["instance_locked"]:
            try:
                await _to_thread(persistence.probe)
            except Exception:
                state["storage_ready"] = False
            else:
                state["storage_ready"] = True
        if (
            needs_slack
            and runtime_client is not None
            and monotonic_clock() - float(state["last_connectivity_check"]) >= 60
        ):
            assert settings.team_id is not None
            try:
                await runtime_client.verify_auth(expected_team_id=settings.team_id)
            except SlackProtocolError:
                state["connectivity_verified"] = False
                if worker is not None:
                    worker.halt()
            else:
                state["connectivity_verified"] = True
                await ensure_worker_started()
                if worker is not None and (
                    not requires_storage or state["storage_ready"]
                ):
                    worker.resume_after_verified_connectivity()
            state["last_connectivity_check"] = monotonic_clock()
        worker_healthy = worker is None or worker.healthy
        ready_now = (not requires_storage or state["storage_ready"]) and (
            not needs_slack or state["connectivity_verified"]
        )
        if settings.notifications_enabled:
            ready_now = ready_now and state["worker_running"] and worker_healthy
        mode = "inactive"
        if requires_storage and not state["storage_ready"]:
            mode = "storage_unavailable"
        elif settings.notifications_enabled and ready_now:
            mode = "operational"
        elif settings.notifications_enabled and not worker_healthy:
            mode = "outbound_halted"
        elif settings.ingress_enabled and ready_now:
            mode = "admission_only"
        elif needs_slack and state["connectivity_verified"]:
            mode = "connectivity_verified"
        elif needs_slack:
            mode = "connectivity_failed"
        inventory = {
            "pending": 0,
            "claimed": 0,
            "request_started": 0,
            "delivery_unknown": 0,
        }
        activation: dict[str, object] = {
            "mode": "unavailable",
            "generation": 0,
            "budget": None,
            "consumed": 0,
            "verified": False,
        }
        if state["storage_ready"]:
            try:
                inventory = await _to_thread(persistence.state_inventory)
                activation = await _to_thread(persistence.activation_status)
            except Exception:
                state["storage_ready"] = False
                ready_now = False
                mode = "storage_unavailable"
        if settings.notifications_enabled and inventory["delivery_unknown"]:
            if worker is not None:
                worker.halt("delivery_unknown")
            worker_healthy = False
            ready_now = False
            mode = "outbound_halted"
        payload = {
            "status": "ok" if ready_now else "not_ready",
            "mode": mode,
            "ingress_enabled": settings.ingress_enabled,
            "notifications_enabled": settings.notifications_enabled,
            "interactions_enabled": settings.interactions_enabled,
            "connectivity_check_enabled": settings.connectivity_check_enabled,
            "bot_token_configured": settings.bot_token is not None,
            "signing_secret_configured": settings.signing_secret is not None,
            "team_configured": settings.team_id is not None,
            "channel_configured": settings.channel_id is not None,
            "storage_ready": state["storage_ready"],
            "tenant_count": len(settings.tenant_tokens),
            "worker_running": state["worker_running"] and worker_healthy,
            "ledger": inventory,
            "activation": activation,
        }
        return JSONResponse(status_code=200 if ready_now else 503, content=payload)

    @app.post("/internal/v1/notifications")
    async def admit_notification(request: Request) -> JSONResponse:
        if not settings.ingress_enabled:
            return JSONResponse(status_code=404, content={"detail": "not_found"})
        tenant_ref = _authenticate_tenant(
            request.headers.get("authorization"), settings.tenant_tokens
        )
        if tenant_ref is None:
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        if not state["storage_ready"]:
            return JSONResponse(
                status_code=503,
                content={"detail": "storage_unavailable"},
            )
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return JSONResponse(status_code=415, content={"detail": "unsupported_media_type"})
        try:
            body = await _read_bounded_body(request, limit=_MAX_BODY_BYTES)
        except PayloadTooLarge:
            return JSONResponse(status_code=413, content={"detail": "payload_too_large"})
        try:
            command = _parse_command(body)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid_notification"})
        try:
            result = await _to_thread(
                persistence.admit,
                tenant_ref=tenant_ref,
                command=command,
                max_nonterminal=settings.max_nonterminal_notifications,
            )
        except NotificationCapacityError:
            return JSONResponse(
                status_code=429,
                content={"detail": "queue_capacity_exhausted"},
                headers={"Retry-After": "30"},
            )
        except Exception:
            state["storage_ready"] = False
            return JSONResponse(
                status_code=503,
                content={"detail": "storage_unavailable"},
            )
        status_code = {
            "admitted": 202,
            "duplicate": 200,
            "semantic_conflict": 409,
        }[result.outcome]
        return JSONResponse(
            status_code=status_code,
            content={
                "status": result.outcome,
                "notification_id": result.notification_id,
                "delivery_state": result.state,
            },
        )

    @app.get("/internal/v1/notifications/{notification_id}")
    async def notification_status(notification_id: str, request: Request) -> JSONResponse:
        if not settings.ingress_enabled:
            return JSONResponse(status_code=404, content={"detail": "not_found"})
        tenant_ref = _authenticate_tenant(
            request.headers.get("authorization"), settings.tenant_tokens
        )
        if tenant_ref is None:
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        if not state["storage_ready"]:
            return JSONResponse(
                status_code=503,
                content={"detail": "storage_unavailable"},
            )
        try:
            if str(UUID(notification_id)) != notification_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return JSONResponse(status_code=404, content={"detail": "not_found"})
        try:
            record = await _to_thread(
                persistence.get,
                tenant_ref=tenant_ref,
                notification_id=notification_id,
            )
        except Exception:
            state["storage_ready"] = False
            return JSONResponse(
                status_code=503,
                content={"detail": "storage_unavailable"},
            )
        if record is None:
            return JSONResponse(status_code=404, content={"detail": "not_found"})
        return JSONResponse(
            status_code=200,
            content={
                "notification_id": record.notification_id,
                "event_code": record.event_code,
                "delivery_state": record.state,
                "failure_code": record.failure_code,
                "message_ts": record.message_ts,
                "thread_ts": record.thread_ts,
            },
        )

    @app.post("/internal/v1/operator/reconcile-delivery")
    async def reconcile_delivery(request: Request) -> JSONResponse:
        if settings.operator_bearer_token is None:
            return JSONResponse(status_code=404, content={"detail": "not_found"})
        if not _authenticate_operator(
            request.headers.get("authorization"), settings.operator_bearer_token
        ):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        if not state["storage_ready"]:
            return JSONResponse(status_code=503, content={"detail": "storage_unavailable"})
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return JSONResponse(status_code=415, content={"detail": "unsupported_media_type"})
        try:
            body = await _read_bounded_body(request, limit=2048)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError
            decision = payload.get("decision")
            allowed = {"decision", "tenant_ref", "notification_id"}
            if decision == "confirm_delivered":
                allowed |= {"message_ts", "thread_ts"}
            if set(payload) != allowed or decision not in {
                "confirm_delivered",
                "confirm_not_delivered",
            }:
                raise ValueError
            tenant_ref = payload["tenant_ref"]
            notification_id = payload["notification_id"]
            if tenant_ref not in _ALLOWED_TENANTS or str(UUID(notification_id)) != notification_id:
                raise ValueError
            message_ts = payload.get("message_ts")
            thread_ts = payload.get("thread_ts")
            if decision == "confirm_delivered" and (
                not isinstance(message_ts, str)
                or _SLACK_TS.fullmatch(message_ts) is None
                or (thread_ts is not None and (
                    not isinstance(thread_ts, str) or _SLACK_TS.fullmatch(thread_ts) is None
                ))
            ):
                raise ValueError
        except (PayloadTooLarge, ValueError, KeyError, TypeError, AttributeError):
            return JSONResponse(status_code=400, content={"detail": "invalid_reconciliation"})
        assert settings.channel_id is not None
        try:
            result = await _to_thread(
                persistence.reconcile_delivery_unknown,
                tenant_ref=tenant_ref,
                notification_id=notification_id,
                decision=decision,
                operator_id="operator",
                channel_id=settings.channel_id,
                message_ts=message_ts,
                thread_ts=thread_ts,
            )
        except RuntimeError:
            return JSONResponse(status_code=409, content={"detail": "reconciliation_conflict"})
        if worker is not None and state["connectivity_verified"]:
            worker.resume_after_verified_connectivity()
        return JSONResponse(
            status_code=200,
            content={
                "status": "reconciled",
                "notification_id": result.notification_id,
                "delivery_state": result.state,
            },
        )

    @app.post("/internal/v1/operator/verify-activation")
    async def verify_activation(request: Request) -> JSONResponse:
        if settings.operator_bearer_token is None:
            return JSONResponse(status_code=404, content={"detail": "not_found"})
        if not _authenticate_operator(
            request.headers.get("authorization"), settings.operator_bearer_token
        ):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        try:
            payload = json.loads(await _read_bounded_body(request, limit=256))
            if set(payload) != {"generation"} or isinstance(payload["generation"], bool):
                raise ValueError
            generation = int(payload["generation"])
            if generation != payload["generation"]:
                raise ValueError
            await _to_thread(
                persistence.mark_activation_verified,
                generation=generation,
                operator_id="operator",
            )
        except (PayloadTooLarge, ValueError, TypeError, KeyError):
            return JSONResponse(status_code=400, content={"detail": "invalid_activation_verification"})
        except RuntimeError:
            return JSONResponse(status_code=409, content={"detail": "activation_not_verifiable"})
        return JSONResponse(status_code=200, content={"status": "verified", "generation": generation})

    return app


def build_app() -> FastAPI:
    """Uvicorn factory used by the dedicated connector image."""

    return create_app(SlackConnectorSettings.from_env())


def _authenticate_tenant(
    authorization: str | None,
    tenant_tokens: dict[str, str],
) -> str | None:
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    supplied = authorization[7:]
    matched: str | None = None
    for tenant_ref, expected in sorted(tenant_tokens.items()):
        if compare_digest(supplied, expected):
            matched = tenant_ref
    return matched


def _authenticate_operator(authorization: str | None, expected: str) -> bool:
    return (
        authorization is not None
        and authorization.startswith("Bearer ")
        and compare_digest(authorization[7:], expected)
    )


def _parse_command(body: bytes) -> NotificationCommand:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_notification") from exc
    allowed = {
        "event_id",
        "event_code",
        "dedupe_key",
        "occurred_at",
        "subject_ref",
        "reason_code",
        "component",
        "state",
        "count",
        "deadline_at",
    }
    required = {"event_id", "event_code", "dedupe_key", "occurred_at"}
    if not isinstance(payload, dict) or set(payload) - allowed or not required <= set(payload):
        raise ValueError("invalid_notification")
    if any(key in payload and payload[key] is not None and not isinstance(payload[key], str) for key in ("event_id", "event_code", "dedupe_key", "occurred_at", "subject_ref", "reason_code", "component", "state", "deadline_at")):
        raise ValueError("invalid_notification")
    count = payload.get("count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        raise ValueError("invalid_notification")
    occurred_at = _parse_utc_timestamp(payload["occurred_at"])
    deadline_at = (
        _parse_utc_timestamp(payload["deadline_at"])
        if payload.get("deadline_at") is not None
        else None
    )
    return NotificationCommand(
        event_id=payload["event_id"],
        event_code=payload["event_code"],
        dedupe_key=payload["dedupe_key"],
        occurred_at=occurred_at,
        subject_ref=payload.get("subject_ref"),
        reason_code=payload.get("reason_code"),
        component=payload.get("component"),
        state=payload.get("state"),
        count=count,
        deadline_at=deadline_at,
    )


def _parse_utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid_timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed


async def _read_bounded_body(request: Any, *, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise PayloadTooLarge
    return bytes(body)


async def _to_thread(function, /, *args, **kwargs):
    return await __import__("asyncio").to_thread(function, *args, **kwargs)
