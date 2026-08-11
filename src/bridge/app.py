"""ASGI application for the Chatwoot webhook bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable, Protocol
from urllib.parse import urlparse

import httpx
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)

from bridge.chatwoot import (
    ChatwootClient,
    ChatwootHistoryScanLimitError,
    ChatwootProtocolError,
    ChatwootReplyDeliveryUnknownError,
)
from bridge.chatwoot_inbox import (
    ChatwootWorker,
    DurableChatwootInbox,
    RetryableChatwootWorkError,
)
from bridge.filtering import EventDecision, classify_chatwoot_event
from bridge.hermes import HermesShadowProcessor
from bridge.hotmart import (
    EVENT_CART_ABANDONMENT,
    EVENT_PURCHASE_APPROVED,
    classify_hotmart_event,
    is_stale_event,
    parse_hotmart_purchase_payload,
    parse_hotmart_payload,
    verify_hotmart_token,
)
from bridge.messaging import (
    ChatwootMessageSender,
    MessageSender,
    WhatsAppTemplateConfig,
)
from bridge.opt_out import detect_explicit_opt_out
from bridge.recovery_agent import RecoveryAgentClient
from bridge.reply_splitter import (
    HermesReplySplitter,
    ReplySplitManifestConflictError,
    ReplySplitManifestStorageError,
    validate_reply_parts,
)
from bridge.security import verify_chatwoot_signature
from bridge.supabase import PilotBoundaryConfig, SupabaseClient, SupabaseError
from bridge.worker import (
    DurableDispatcher,
    HumanHandoffProjectionWorker,
    OptOutProjectionWorker,
    ResolutionWorker,
)

logger = logging.getLogger(__name__)
CHATWOOT_WEBHOOK_BODY_LIMIT_BYTES = 1024 * 1024
HOTMART_WEBHOOK_BODY_LIMIT_BYTES = 1024 * 1024


class CanonicalHistoryIncompleteError(RetryableChatwootWorkError):
    """Raised when Chatwoot has not exposed the triggering message yet."""


@dataclass(frozen=True)
class CanonicalWorkResult:
    proposal: dict[str, object] | None
    stopped: bool = False


class ChatwootControl(Protocol):
    async def validate_conversation_authority(
        self, *, conversation_id: int, expected_inbox_id: int
    ) -> None: ...

    async def get_conversation_messages(
        self,
        *,
        conversation_id: int,
        limit: int = 20,
        required_message_ids: tuple[int, ...] = (),
    ) -> list[dict[str, object]]: ...

    async def ensure_conversation_label(
        self, *, conversation_id: int, label: str
    ) -> bool: ...

    async def apply_opt_out_macro(self, *, conversation_id: int) -> None: ...

    async def send_agent_bot_reply(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        delivery_id: str,
        content: str,
        part_index: int = 1,
        part_count: int = 1,
        prior_parts: tuple[str, ...] = (),
    ) -> dict[str, object]: ...


class ShadowProcessor(Protocol):
    async def run(
        self, *, delivery_id: str, context: dict[str, object]
    ) -> None: ...

    def record_failure(self, *, delivery_id: str, reason: str) -> None: ...

    def has_result(self, *, delivery_id: str) -> bool: ...

    def get_completed_proposal(
        self, *, delivery_id: str
    ) -> dict[str, object] | None: ...


class ReplySplitter(Protocol):
    async def split(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        reply: str,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    allowed_jid: str
    capture_dir: Path
    max_age_seconds: int
    agent_bot_id: int | None = None
    chatwoot_base_url: str | None = None
    chatwoot_account_id: int | None = None
    chatwoot_control_api_access_token: str | None = None
    chatwoot_agent_bot_access_token: str | None = None
    chatwoot_pause_macro_id: int | None = None
    chatwoot_opt_out_macro_id: int | None = None
    hermes_shadow_enabled: bool = False
    hermes_api_base_url: str | None = None
    hermes_api_key: str | None = None
    hermes_model_name: str = "agente-comercial"
    shadow_dir: Path = Path("./data/shadow")
    automated_replies_enabled: bool = False
    reply_dir: Path = Path("./data/replies")
    reply_splitter_enabled: bool = False
    reply_splitter_provider: str | None = None
    reply_splitter_model_name: str | None = None
    reply_part_delay_seconds: float = 2.0
    chatwoot_inbound_debounce_seconds: float = 0.0
    hotmart_hottok: str | None = None
    hotmart_max_age_seconds: int = 300
    hotmart_purchase_worker_enabled: bool = False
    supabase_base_url: str | None = None
    supabase_service_role_key: str | None = None
    worker_poll_interval_seconds: float = 5.0
    worker_batch_size: int = 10
    worker_enabled: bool = False
    chatwoot_inbox_id: int | None = None
    messaging_channel: str = "evolution"
    followup_policy_key: str | None = None
    followup_policy_version: int | None = None
    dispatcher_enabled: bool = False
    dispatcher_worker_id: str | None = None
    dispatcher_poll_interval_seconds: float = 5.0
    dispatcher_batch_size: int = 10
    dispatcher_outbound_enabled: bool = False
    chatwoot_durable_opt_out_enabled: bool = False
    opt_out_projection_worker_id: str | None = None
    human_handoff_projection_enabled: bool = False
    human_handoff_admission_enabled: bool = False
    handoff_projection_policy_key: str | None = None
    handoff_projection_policy_version: int | None = None
    human_handoff_projection_worker_id: str | None = None
    human_handoff_projection_poll_interval_seconds: float = 5.0
    human_handoff_projection_batch_size: int = 10
    human_handoff_projection_lease_seconds: int = 60
    human_handoff_projection_max_attempts: int = 8
    pilot_boundary_enabled: bool = False
    pilot_scope_key: str | None = None
    pilot_scope_version: int | None = None
    pilot_tenant_key: str | None = None
    pilot_channel_provider: str | None = None
    pilot_channel_account_ref: str | None = None
    waba_first_touch_template_name: str | None = None
    waba_followup_template_name: str | None = None
    waba_template_language: str | None = None
    waba_template_category: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        shadow_enabled = os.getenv("HERMES_SHADOW_ENABLED", "false").lower() == "true"
        automated_replies_enabled = (
            os.getenv("CHATWOOT_AUTOMATED_REPLIES_ENABLED", "false").lower()
            == "true"
        )
        reply_splitter_enabled = (
            os.getenv("CHATWOOT_REPLY_SPLITTER_ENABLED", "false").lower()
            == "true"
        )
        reply_part_delay_seconds = float(
            os.getenv("CHATWOOT_REPLY_PART_DELAY_SECONDS", "2")
        )
        if (
            not math.isfinite(reply_part_delay_seconds)
            or reply_part_delay_seconds < 0
        ):
            raise ValueError(
                "CHATWOOT_REPLY_PART_DELAY_SECONDS must be finite and not negative"
            )
        chatwoot_inbound_debounce_seconds = float(
            os.getenv("CHATWOOT_INBOUND_DEBOUNCE_SECONDS", "30")
        )
        if (
            not math.isfinite(chatwoot_inbound_debounce_seconds)
            or chatwoot_inbound_debounce_seconds < 0
        ):
            raise ValueError(
                "CHATWOOT_INBOUND_DEBOUNCE_SECONDS must be finite and not negative"
            )
        agent_bot_access_token = (
            os.getenv("CHATWOOT_AGENT_BOT_ACCESS_TOKEN", "").strip() or None
        )
        if automated_replies_enabled and not shadow_enabled:
            raise ValueError(
                "CHATWOOT_AUTOMATED_REPLIES_ENABLED requires HERMES_SHADOW_ENABLED"
            )
        if automated_replies_enabled and agent_bot_access_token is None:
            raise ValueError(
                "CHATWOOT_AGENT_BOT_ACCESS_TOKEN is required for automated replies"
            )
        reply_splitter_provider = (
            os.getenv("HERMES_REPLY_SPLITTER_PROVIDER", "").strip() or None
        )
        reply_splitter_model_name = (
            os.getenv("HERMES_REPLY_SPLITTER_MODEL_NAME", "").strip() or None
        )
        if reply_splitter_enabled and not automated_replies_enabled:
            raise ValueError(
                "CHATWOOT_REPLY_SPLITTER_ENABLED requires "
                "CHATWOOT_AUTOMATED_REPLIES_ENABLED"
            )
        if reply_splitter_enabled and reply_splitter_provider is None:
            raise ValueError("HERMES_REPLY_SPLITTER_PROVIDER is required")
        if reply_splitter_enabled and reply_splitter_model_name is None:
            raise ValueError("HERMES_REPLY_SPLITTER_MODEL_NAME is required")
        hermes_model_name = os.getenv(
            "HERMES_MODEL_NAME", "agente-comercial"
        ).strip()
        if shadow_enabled:
            hermes_api_base_url = os.environ["HERMES_API_BASE_URL"].strip()
            hermes_api_key = os.environ["HERMES_API_KEY"].strip()
            if not hermes_api_base_url:
                raise ValueError("HERMES_API_BASE_URL must not be blank")
            if not hermes_api_key:
                raise ValueError("HERMES_API_KEY must not be blank")
            if not hermes_model_name:
                raise ValueError("HERMES_MODEL_NAME must not be blank")
            parsed_hermes_url = urlparse(hermes_api_base_url)
            if parsed_hermes_url.hostname is None:
                raise ValueError(
                    "HERMES_API_BASE_URL must include a valid hostname"
                )
            if (
                parsed_hermes_url.username is not None
                or parsed_hermes_url.password is not None
            ):
                raise ValueError(
                    "HERMES_API_BASE_URL must not contain credentials"
                )
            if parsed_hermes_url.query or parsed_hermes_url.fragment:
                raise ValueError(
                    "HERMES_API_BASE_URL must not contain query or fragment"
                )
            try:
                parsed_hermes_url.port
            except ValueError as exc:
                raise ValueError(
                    "HERMES_API_BASE_URL must include a valid port"
                ) from exc
            trusted_http_hosts = {"hermes", "localhost", "127.0.0.1", "::1"}
            if parsed_hermes_url.scheme != "https" and not (
                parsed_hermes_url.scheme == "http"
                and parsed_hermes_url.hostname in trusted_http_hosts
            ):
                raise ValueError(
                    "HERMES_API_BASE_URL must use HTTPS or trusted internal HTTP"
                )
        else:
            hermes_api_base_url = os.getenv("HERMES_API_BASE_URL") or None
            hermes_api_key = os.getenv("HERMES_API_KEY") or None

        hotmart_hottok = os.getenv("HOTMART_HOTTOK", "").strip() or None
        hotmart_max_age_seconds = int(
            os.getenv("HOTMART_MAX_AGE_SECONDS", "300")
        )
        supabase_base_url = os.getenv("SUPABASE_BASE_URL", "").strip() or None
        supabase_service_role_key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip() or None
        )
        worker_enabled = (
            os.getenv("RESOLUTION_WORKER_ENABLED", "false").lower() == "true"
        )
        hotmart_purchase_worker_enabled = (
            os.getenv("HOTMART_PURCHASE_WORKER_ENABLED", "false").lower()
            == "true"
        )
        if hotmart_purchase_worker_enabled and not worker_enabled:
            raise ValueError(
                "HOTMART_PURCHASE_WORKER_ENABLED requires "
                "RESOLUTION_WORKER_ENABLED"
            )
        worker_poll_interval = float(
            os.getenv("RESOLUTION_WORKER_POLL_INTERVAL", "5.0")
        )
        worker_batch_size = int(
            os.getenv("RESOLUTION_WORKER_BATCH_SIZE", "10")
        )
        chatwoot_inbox_id_raw = os.getenv("CHATWOOT_INBOX_ID", "").strip()
        chatwoot_inbox_id = int(chatwoot_inbox_id_raw) if chatwoot_inbox_id_raw else None
        messaging_channel = os.getenv("MESSAGING_CHANNEL", "evolution").strip().lower()
        followup_policy_key = os.getenv("FOLLOWUP_POLICY_KEY", "").strip() or None
        followup_policy_version_raw = os.getenv(
            "FOLLOWUP_POLICY_VERSION", ""
        ).strip()
        followup_policy_version = (
            int(followup_policy_version_raw)
            if followup_policy_version_raw
            else None
        )
        dispatcher_enabled = (
            os.getenv("DURABLE_DISPATCHER_ENABLED", "false").lower() == "true"
        )
        dispatcher_worker_id = (
            os.getenv("DURABLE_DISPATCHER_WORKER_ID", "").strip() or None
        )
        dispatcher_poll_interval_seconds = float(
            os.getenv("DURABLE_DISPATCHER_POLL_INTERVAL", "5.0")
        )
        dispatcher_batch_size = int(
            os.getenv("DURABLE_DISPATCHER_BATCH_SIZE", "10")
        )
        dispatcher_outbound_enabled = (
            os.getenv("DURABLE_OUTBOUND_ENABLED", "false").lower() == "true"
        )
        chatwoot_durable_opt_out_enabled = (
            os.getenv("CHATWOOT_DURABLE_OPT_OUT_ENABLED", "false").lower()
            == "true"
        )
        opt_out_macro_id_raw = os.getenv("CHATWOOT_OPT_OUT_MACRO_ID", "").strip()
        chatwoot_opt_out_macro_id = (
            int(opt_out_macro_id_raw) if opt_out_macro_id_raw else None
        )
        opt_out_projection_worker_id = (
            os.getenv("CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID", "").strip() or None
        )
        human_handoff_projection_enabled = (
            os.getenv("HUMAN_HANDOFF_PROJECTION_ENABLED", "false").lower()
            == "true"
        )
        human_handoff_admission_enabled = (
            os.getenv("HUMAN_HANDOFF_ADMISSION_ENABLED", "false").lower()
            == "true"
        )
        handoff_projection_policy_key = (
            os.getenv("HANDOFF_PROJECTION_POLICY_KEY", "").strip() or None
        )
        raw_handoff_policy_version = os.getenv(
            "HANDOFF_PROJECTION_POLICY_VERSION", ""
        ).strip()
        handoff_projection_policy_version = (
            int(raw_handoff_policy_version) if raw_handoff_policy_version else None
        )
        human_handoff_projection_worker_id = (
            os.getenv("HUMAN_HANDOFF_PROJECTION_WORKER_ID", "").strip() or None
        )
        pilot_boundary_enabled = (
            os.getenv("LANCEMOS_PILOT_BOUNDARY_ENABLED", "false").lower()
            == "true"
        )
        pilot_scope_version_raw = os.getenv(
            "LANCEMOS_PILOT_SCOPE_VERSION", ""
        ).strip()

        return cls(
            webhook_secret=os.environ["CHATWOOT_WEBHOOK_SECRET"],
            allowed_jid=os.environ["ALLOWED_WHATSAPP_JID"],
            capture_dir=Path(os.getenv("CAPTURE_DIR", "./data/captures")),
            max_age_seconds=int(os.getenv("WEBHOOK_MAX_AGE_SECONDS", "300")),
            agent_bot_id=int(os.environ["CHATWOOT_AGENT_BOT_ID"]),
            chatwoot_base_url=os.environ["CHATWOOT_BASE_URL"],
            chatwoot_account_id=int(os.environ["CHATWOOT_ACCOUNT_ID"]),
            chatwoot_control_api_access_token=os.environ[
                "CHATWOOT_CONTROL_API_ACCESS_TOKEN"
            ],
            chatwoot_agent_bot_access_token=agent_bot_access_token,
            chatwoot_pause_macro_id=int(os.environ["CHATWOOT_PAUSE_MACRO_ID"]),
            chatwoot_opt_out_macro_id=chatwoot_opt_out_macro_id,
            hermes_shadow_enabled=shadow_enabled,
            hermes_api_base_url=hermes_api_base_url,
            hermes_api_key=hermes_api_key,
            hermes_model_name=hermes_model_name,
            shadow_dir=Path(os.getenv("SHADOW_DIR", "./data/shadow")),
            automated_replies_enabled=automated_replies_enabled,
            reply_dir=Path(os.getenv("REPLY_DIR", "./data/replies")),
            reply_splitter_enabled=reply_splitter_enabled,
            reply_splitter_provider=reply_splitter_provider,
            reply_splitter_model_name=reply_splitter_model_name,
            reply_part_delay_seconds=reply_part_delay_seconds,
            chatwoot_inbound_debounce_seconds=(
                chatwoot_inbound_debounce_seconds
            ),
            hotmart_hottok=hotmart_hottok,
            hotmart_max_age_seconds=hotmart_max_age_seconds,
            hotmart_purchase_worker_enabled=hotmart_purchase_worker_enabled,
            supabase_base_url=supabase_base_url,
            supabase_service_role_key=supabase_service_role_key,
            worker_poll_interval_seconds=worker_poll_interval,
            worker_batch_size=worker_batch_size,
            worker_enabled=worker_enabled,
            chatwoot_inbox_id=chatwoot_inbox_id,
            messaging_channel=messaging_channel,
            followup_policy_key=followup_policy_key,
            followup_policy_version=followup_policy_version,
            dispatcher_enabled=dispatcher_enabled,
            dispatcher_worker_id=dispatcher_worker_id,
            dispatcher_poll_interval_seconds=dispatcher_poll_interval_seconds,
            dispatcher_batch_size=dispatcher_batch_size,
            dispatcher_outbound_enabled=dispatcher_outbound_enabled,
            chatwoot_durable_opt_out_enabled=chatwoot_durable_opt_out_enabled,
            opt_out_projection_worker_id=opt_out_projection_worker_id,
            human_handoff_projection_enabled=human_handoff_projection_enabled,
            human_handoff_admission_enabled=human_handoff_admission_enabled,
            handoff_projection_policy_key=handoff_projection_policy_key,
            handoff_projection_policy_version=handoff_projection_policy_version,
            human_handoff_projection_worker_id=(
                human_handoff_projection_worker_id
            ),
            human_handoff_projection_poll_interval_seconds=float(
                os.getenv("HUMAN_HANDOFF_PROJECTION_POLL_INTERVAL", "5.0")
            ),
            human_handoff_projection_batch_size=int(
                os.getenv("HUMAN_HANDOFF_PROJECTION_BATCH_SIZE", "10")
            ),
            human_handoff_projection_lease_seconds=int(
                os.getenv("HUMAN_HANDOFF_PROJECTION_LEASE_SECONDS", "60")
            ),
            human_handoff_projection_max_attempts=int(
                os.getenv("HUMAN_HANDOFF_PROJECTION_MAX_ATTEMPTS", "8")
            ),
            pilot_boundary_enabled=pilot_boundary_enabled,
            pilot_scope_key=(
                os.getenv("LANCEMOS_PILOT_SCOPE_KEY", "").strip() or None
            ),
            pilot_scope_version=(
                int(pilot_scope_version_raw) if pilot_scope_version_raw else None
            ),
            pilot_tenant_key=(
                os.getenv("LANCEMOS_PILOT_TENANT_KEY", "").strip() or None
            ),
            pilot_channel_provider=(
                os.getenv("LANCEMOS_PILOT_CHANNEL_PROVIDER", "").strip() or None
            ),
            pilot_channel_account_ref=(
                os.getenv("LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF", "").strip() or None
            ),
            waba_first_touch_template_name=(
                os.getenv("WABA_FIRST_TOUCH_TEMPLATE_NAME", "").strip() or None
            ),
            waba_followup_template_name=(
                os.getenv("WABA_FOLLOWUP_TEMPLATE_NAME", "").strip() or None
            ),
            waba_template_language=(
                os.getenv("WABA_TEMPLATE_LANGUAGE", "").strip() or None
            ),
            waba_template_category=(
                os.getenv("WABA_TEMPLATE_CATEGORY", "").strip().upper() or None
            ),
        )


def _capture_payload(
    *, capture_dir: Path, delivery_id: str, payload: dict[str, object]
) -> bool:
    capture_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory_fd = os.open(
        capture_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    os.fchmod(directory_fd, 0o700)
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    capture_path = capture_dir / f"{digest}.json"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.", suffix=".tmp", dir=capture_dir
    )
    try:
        os.fchmod(temporary_fd, 0o600)
        handle = os.fdopen(temporary_fd, "w", encoding="utf-8")
        temporary_fd = -1
        with handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, capture_path)
        except FileExistsError:
            return False
        os.fsync(directory_fd)
        return True
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        os.close(directory_fd)
        os.unlink(temporary_name)


def _shadow_context(payload: dict[str, object]) -> dict[str, object] | None:
    conversation = payload.get("conversation")
    content = payload.get("content")
    if not isinstance(conversation, dict):
        return None
    conversation_id = conversation.get("id")
    if (
        not isinstance(conversation_id, int)
        or isinstance(conversation_id, bool)
        or conversation_id <= 0
    ):
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    return {
        "conversation_ref": str(conversation_id),
        "human_handoff_confirmed": False,
        "known_fields": {
            "person_name": None,
            "location": None,
            "role": None,
            "company_name": None,
            "company_size": None,
            "business_model": None,
            "company_operational": None,
            "can_invest_in_education": None,
        },
        "messages": [
            {
                "actor": "prospect",
                "text": content.strip(),
            }
        ],
    }


def _normalize_chatwoot_history(
    messages: list[dict[str, object]], *, agent_bot_id: int
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for message in messages:
        if message.get("private") is not False:
            continue
        content = message.get("content")
        sender = message.get("sender")
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(sender, dict):
            continue

        actor: str | None = None
        if message.get("message_type") == 0 and sender.get("type") == "contact":
            actor = "prospect"
        elif (
            message.get("message_type") == 1
            and sender.get("type") == "agent_bot"
            and sender.get("id") == agent_bot_id
        ):
            actor = "assistant"
        if actor is not None:
            normalized_message = {"actor": actor, "text": content.strip()}
            message_id = message.get("id")
            if isinstance(message_id, int) and not isinstance(message_id, bool):
                normalized_message["_message_ref"] = str(message_id)
            created_at = message.get("created_at")
            if (
                isinstance(created_at, int)
                and not isinstance(created_at, bool)
                and created_at > 0
            ):
                normalized_message["_created_at"] = str(created_at)
            normalized.append(normalized_message)
    return normalized


def create_app(
    settings: Settings,
    *,
    chatwoot_client: ChatwootControl | None = None,
    shadow_processor: ShadowProcessor | None = None,
    reply_splitter: ReplySplitter | None = None,
    reply_part_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    supabase_client: SupabaseClient | None = None,
    recovery_agent_client: RecoveryAgentClient | None = None,
    message_sender: MessageSender | None = None,
) -> FastAPI:
    pilot_fields = (
        (settings.pilot_scope_key, "LANCEMOS_PILOT_SCOPE_KEY"),
        (settings.pilot_scope_version, "LANCEMOS_PILOT_SCOPE_VERSION"),
        (settings.pilot_tenant_key, "LANCEMOS_PILOT_TENANT_KEY"),
        (settings.pilot_channel_provider, "LANCEMOS_PILOT_CHANNEL_PROVIDER"),
        (settings.pilot_channel_account_ref, "LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF"),
    )
    if settings.pilot_boundary_enabled:
        for value, name in pilot_fields:
            if value is None or value == "":
                raise ValueError(f"{name} is required when pilot boundary is enabled")
        if settings.pilot_scope_version is None or settings.pilot_scope_version < 1:
            raise ValueError("LANCEMOS_PILOT_SCOPE_VERSION must be positive")
    if settings.dispatcher_outbound_enabled and not settings.pilot_boundary_enabled:
        raise ValueError(
            "DURABLE_OUTBOUND_ENABLED requires LANCEMOS_PILOT_BOUNDARY_ENABLED"
        )
    waba_template: WhatsAppTemplateConfig | None = None
    if (
        settings.dispatcher_outbound_enabled
        and settings.pilot_channel_provider == "waba"
    ):
        template_fields = (
            (settings.waba_first_touch_template_name, "WABA_FIRST_TOUCH_TEMPLATE_NAME"),
            (settings.waba_followup_template_name, "WABA_FOLLOWUP_TEMPLATE_NAME"),
            (settings.waba_template_language, "WABA_TEMPLATE_LANGUAGE"),
            (settings.waba_template_category, "WABA_TEMPLATE_CATEGORY"),
        )
        for value, name in template_fields:
            if value is None or not value.strip():
                raise ValueError(f"{name} is required for WABA outbound")
        if settings.waba_template_category not in {"MARKETING", "UTILITY"}:
            raise ValueError("WABA_TEMPLATE_CATEGORY must be MARKETING or UTILITY")
        waba_template = WhatsAppTemplateConfig(
            first_touch_name=settings.waba_first_touch_template_name,  # type: ignore[arg-type]
            followup_name=settings.waba_followup_template_name,  # type: ignore[arg-type]
            language=settings.waba_template_language,  # type: ignore[arg-type]
            category=settings.waba_template_category,  # type: ignore[arg-type]
        )
    pilot_boundary = (
        PilotBoundaryConfig(
            scope_key=settings.pilot_scope_key,  # type: ignore[arg-type]
            scope_version=settings.pilot_scope_version,  # type: ignore[arg-type]
            tenant_key=settings.pilot_tenant_key,  # type: ignore[arg-type]
            channel_provider=settings.pilot_channel_provider,  # type: ignore[arg-type]
            channel_account_ref=settings.pilot_channel_account_ref,  # type: ignore[arg-type]
        )
        if settings.pilot_boundary_enabled
        else None
    )
    if settings.hotmart_purchase_worker_enabled and not settings.worker_enabled:
        raise ValueError(
            "HOTMART_PURCHASE_WORKER_ENABLED requires "
            "RESOLUTION_WORKER_ENABLED"
        )
    if (
        not math.isfinite(settings.chatwoot_inbound_debounce_seconds)
        or settings.chatwoot_inbound_debounce_seconds < 0
    ):
        raise ValueError(
            "CHATWOOT_INBOUND_DEBOUNCE_SECONDS must be finite and not negative"
        )
    if (
        not math.isfinite(settings.reply_part_delay_seconds)
        or settings.reply_part_delay_seconds < 0
    ):
        raise ValueError(
            "CHATWOOT_REPLY_PART_DELAY_SECONDS must be finite and not negative"
        )
    if settings.chatwoot_durable_opt_out_enabled and (
        settings.chatwoot_account_id is None
        or settings.chatwoot_account_id < 1
        or settings.chatwoot_inbox_id is None
        or settings.chatwoot_inbox_id < 1
        or settings.agent_bot_id is None
        or settings.agent_bot_id < 1
        or settings.chatwoot_opt_out_macro_id is None
        or settings.chatwoot_opt_out_macro_id < 1
        or settings.opt_out_projection_worker_id is None
    ):
        raise ValueError(
            "CHATWOOT_DURABLE_OPT_OUT_ENABLED requires canonical Chatwoot IDs"
        )
    control_client = chatwoot_client
    if (
        control_client is None
        and settings.chatwoot_base_url is not None
        and settings.chatwoot_account_id is not None
        and settings.chatwoot_control_api_access_token is not None
        and settings.chatwoot_pause_macro_id is not None
    ):
        control_client = ChatwootClient(
            base_url=settings.chatwoot_base_url,
            account_id=settings.chatwoot_account_id,
            access_token=settings.chatwoot_control_api_access_token,
            allowed_jid=settings.allowed_jid,
            agent_bot_access_token=settings.chatwoot_agent_bot_access_token,
            agent_bot_id=settings.agent_bot_id,
            reply_dir=settings.reply_dir,
            pause_macro_id=settings.chatwoot_pause_macro_id,
            opt_out_macro_id=settings.chatwoot_opt_out_macro_id,
        )
    configured_reply_splitter = reply_splitter
    reply_manifest_reader = HermesReplySplitter(
        base_url="",
        api_key="",
        provider="",
        model_name="",
        result_dir=settings.reply_dir / ".splits",
    )
    if settings.reply_splitter_enabled and configured_reply_splitter is None:
        if (
            settings.hermes_api_base_url is None
            or settings.hermes_api_key is None
            or settings.reply_splitter_provider is None
            or settings.reply_splitter_model_name is None
        ):
            raise ValueError("reply splitter Hermes settings are required")
        configured_reply_splitter = HermesReplySplitter(
            base_url=settings.hermes_api_base_url,
            api_key=settings.hermes_api_key,
            provider=settings.reply_splitter_provider,
            model_name=settings.reply_splitter_model_name,
            result_dir=settings.reply_dir / ".splits",
        )
    chatwoot_inbox = (
        DurableChatwootInbox(Path(settings.capture_dir) / ".work")
        if shadow_processor is not None or control_client is not None
        else None
    )

    # Shared Supabase client (injected or constructed from settings).
    shared_supabase = supabase_client
    if (
        shared_supabase is None
        and settings.supabase_base_url is not None
        and settings.supabase_service_role_key is not None
    ):
        shared_supabase = SupabaseClient(
            base_url=settings.supabase_base_url,
            service_role_key=settings.supabase_service_role_key,
        )
    if settings.chatwoot_durable_opt_out_enabled and (
        shared_supabase is None or control_client is None
    ):
        raise ValueError(
            "CHATWOOT_DURABLE_OPT_OUT_ENABLED requires Supabase and Chatwoot control"
        )
    if settings.human_handoff_admission_enabled:
        if (
            not settings.dispatcher_enabled
            or not settings.dispatcher_outbound_enabled
            or not settings.pilot_boundary_enabled
            or not settings.human_handoff_projection_enabled
        ):
            raise ValueError(
                "HUMAN_HANDOFF_ADMISSION_ENABLED requires outbound dispatcher, "
                "pilot boundary, and handoff projection"
            )
        if (
            not settings.handoff_projection_policy_key
            or settings.handoff_projection_policy_version is None
            or settings.handoff_projection_policy_version < 1
        ):
            raise ValueError(
                "HANDOFF_PROJECTION_POLICY_KEY and "
                "HANDOFF_PROJECTION_POLICY_VERSION are required"
            )
    if settings.human_handoff_projection_enabled:
        if shared_supabase is None or not isinstance(control_client, ChatwootClient):
            raise ValueError(
                "HUMAN_HANDOFF_PROJECTION_ENABLED requires Supabase and "
                "Chatwoot control"
            )
        if not settings.human_handoff_projection_worker_id:
            raise ValueError("HUMAN_HANDOFF_PROJECTION_WORKER_ID is required")
        if (
            settings.chatwoot_account_id is None
            or settings.chatwoot_account_id < 1
            or settings.chatwoot_inbox_id is None
            or settings.chatwoot_inbox_id < 1
        ):
            raise ValueError(
                "HUMAN_HANDOFF_PROJECTION_ENABLED requires canonical Chatwoot IDs"
            )
        if (
            not math.isfinite(
                settings.human_handoff_projection_poll_interval_seconds
            )
            or settings.human_handoff_projection_poll_interval_seconds <= 0
        ):
            raise ValueError(
                "HUMAN_HANDOFF_PROJECTION_POLL_INTERVAL must be positive"
            )
        if not 1 <= settings.human_handoff_projection_batch_size <= 100:
            raise ValueError(
                "HUMAN_HANDOFF_PROJECTION_BATCH_SIZE must be between 1 and 100"
            )
        if not 5 <= settings.human_handoff_projection_lease_seconds <= 900:
            raise ValueError(
                "HUMAN_HANDOFF_PROJECTION_LEASE_SECONDS must be between 5 and 900"
            )
        if not 1 <= settings.human_handoff_projection_max_attempts <= 100:
            raise ValueError(
                "HUMAN_HANDOFF_PROJECTION_MAX_ATTEMPTS must be between 1 and 100"
            )

    # Build background workers only when explicitly enabled.
    resolution_worker: ResolutionWorker | None = None
    durable_dispatcher: DurableDispatcher | None = None
    if settings.worker_enabled and shared_supabase is None:
        raise ValueError("Supabase is required when RESOLUTION_WORKER_ENABLED=true")
    if (
        settings.worker_enabled
        and shared_supabase is not None
    ):
        if (
            settings.followup_policy_key is None
            or settings.followup_policy_version is None
        ):
            raise ValueError(
                "FOLLOWUP_POLICY_KEY and FOLLOWUP_POLICY_VERSION are required "
                "when RESOLUTION_WORKER_ENABLED=true"
            )
        if settings.allowed_jid is None:
            raise ValueError(
                "ALLOWED_WHATSAPP_JID is required when "
                "RESOLUTION_WORKER_ENABLED=true"
            )
        if settings.chatwoot_account_id is None:
            raise ValueError(
                "CHATWOOT_ACCOUNT_ID is required when "
                "RESOLUTION_WORKER_ENABLED=true"
            )
        if settings.chatwoot_inbox_id is None:
            raise ValueError(
                "CHATWOOT_INBOX_ID is required when "
                "RESOLUTION_WORKER_ENABLED=true"
            )
        if not settings.pilot_boundary_enabled:
            raise ValueError(
                "RESOLUTION_WORKER_ENABLED requires "
                "LANCEMOS_PILOT_BOUNDARY_ENABLED"
            )
        recovery_agent = recovery_agent_client
        if (
            recovery_agent is None
            and settings.hermes_api_base_url is not None
            and settings.hermes_api_key is not None
        ):
            recovery_agent = RecoveryAgentClient(
                base_url=settings.hermes_api_base_url,
                api_key=settings.hermes_api_key,
                model_name=settings.hermes_model_name,
                proposals_dir=Path(
                    os.getenv("RECOVERY_PROPOSALS_DIR", "./data/recovery")
                ),
            )
        sender = message_sender
        if (
            sender is None
            and settings.pilot_channel_provider in {"evolution", "waba"}
            and settings.chatwoot_inbox_id is not None
            and control_client is not None
            and isinstance(control_client, ChatwootClient)
        ):
            sender = ChatwootMessageSender(
                chatwoot=control_client,
                inbox_id=settings.chatwoot_inbox_id,
                allowed_jid=settings.allowed_jid,
                template=waba_template,
            )
        resolution_worker = ResolutionWorker(
            supabase=shared_supabase,
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            batch_size=settings.worker_batch_size,
            recovery_agent=recovery_agent,
            message_sender=sender,
            allowed_jid=settings.allowed_jid,
            chatwoot_account_id=settings.chatwoot_account_id,
            chatwoot_inbox_id=settings.chatwoot_inbox_id,
            policy_key=settings.followup_policy_key,
            policy_version=settings.followup_policy_version,
            purchase_worker_enabled=settings.hotmart_purchase_worker_enabled,
            pilot_boundary=pilot_boundary,
        )

    if settings.dispatcher_enabled:
        if shared_supabase is None:
            raise ValueError(
                "Supabase is required when DURABLE_DISPATCHER_ENABLED=true"
            )
        if settings.dispatcher_worker_id is None:
            raise ValueError(
                "DURABLE_DISPATCHER_WORKER_ID is required when "
                "DURABLE_DISPATCHER_ENABLED=true"
            )
        if not isinstance(control_client, ChatwootClient):
            raise ValueError(
                "Chatwoot control API is required when "
                "DURABLE_DISPATCHER_ENABLED=true"
            )
        if settings.chatwoot_account_id is None:
            raise ValueError(
                "CHATWOOT_ACCOUNT_ID is required when "
                "DURABLE_DISPATCHER_ENABLED=true"
            )
        if settings.dispatcher_poll_interval_seconds <= 0:
            raise ValueError("DURABLE_DISPATCHER_POLL_INTERVAL must be positive")
        if not 1 <= settings.dispatcher_batch_size <= 100:
            raise ValueError("DURABLE_DISPATCHER_BATCH_SIZE must be between 1 and 100")
        outbound_agent: RecoveryAgentClient | None = None
        outbound_sender: MessageSender | None = None
        if settings.dispatcher_outbound_enabled:
            outbound_agent = recovery_agent_client
            if (
                outbound_agent is None
                and settings.hermes_api_base_url is not None
                and settings.hermes_api_key is not None
            ):
                outbound_agent = RecoveryAgentClient(
                    base_url=settings.hermes_api_base_url,
                    api_key=settings.hermes_api_key,
                    model_name=settings.hermes_model_name,
                    proposals_dir=Path(
                        os.getenv("RECOVERY_PROPOSALS_DIR", "./data/recovery")
                    ),
                )
            outbound_sender = message_sender
            if (
                outbound_sender is None
                and settings.pilot_channel_provider in {"evolution", "waba"}
                and settings.chatwoot_inbox_id is not None
                and isinstance(control_client, ChatwootClient)
            ):
                outbound_sender = ChatwootMessageSender(
                    chatwoot=control_client,
                    inbox_id=settings.chatwoot_inbox_id,
                    allowed_jid=settings.allowed_jid,
                    template=waba_template,
                )
            if outbound_agent is None or outbound_sender is None:
                raise ValueError(
                    "durable outbound requires Hermes and sender dependencies"
                )
        durable_dispatcher = DurableDispatcher(
            supabase=shared_supabase,
            worker_id=settings.dispatcher_worker_id,
            poll_interval_seconds=settings.dispatcher_poll_interval_seconds,
            batch_size=settings.dispatcher_batch_size,
            chatwoot=control_client,
            chatwoot_account_id=settings.chatwoot_account_id,
            recovery_agent=outbound_agent,
            sender=outbound_sender,
            allowed_jid=(
                settings.allowed_jid
                if settings.dispatcher_outbound_enabled
                else None
            ),
            pilot_boundary=pilot_boundary,
            human_handoff_admission_enabled=(
                settings.human_handoff_admission_enabled
            ),
            handoff_projection_policy_key=settings.handoff_projection_policy_key,
            handoff_projection_policy_version=(
                settings.handoff_projection_policy_version
            ),
        )

    opt_out_projection_worker: OptOutProjectionWorker | None = None
    human_handoff_projection_worker: HumanHandoffProjectionWorker | None = None
    opt_out_enforcement_enabled = (
        shared_supabase is not None
        and control_client is not None
        and settings.agent_bot_id is not None
        and settings.chatwoot_account_id is not None
        and settings.chatwoot_account_id > 0
        and settings.chatwoot_inbox_id is not None
        and settings.chatwoot_inbox_id > 0
    )
    opt_out_projection_configured = (
        opt_out_enforcement_enabled
        and settings.chatwoot_opt_out_macro_id is not None
        and settings.chatwoot_opt_out_macro_id > 0
        and bool(settings.opt_out_projection_worker_id)
    )
    if opt_out_projection_configured:
        assert shared_supabase is not None
        assert control_client is not None
        assert settings.opt_out_projection_worker_id is not None
        opt_out_projection_worker = OptOutProjectionWorker(
            supabase=shared_supabase,
            chatwoot=control_client,  # type: ignore[arg-type]
            worker_id=settings.opt_out_projection_worker_id,
        )
    if settings.human_handoff_projection_enabled:
        assert shared_supabase is not None
        assert isinstance(control_client, ChatwootClient)
        assert settings.human_handoff_projection_worker_id is not None
        human_handoff_projection_worker = HumanHandoffProjectionWorker(
            supabase=shared_supabase,
            chatwoot=control_client,
            worker_id=settings.human_handoff_projection_worker_id,
            poll_interval_seconds=(
                settings.human_handoff_projection_poll_interval_seconds
            ),
            batch_size=settings.human_handoff_projection_batch_size,
            lease_seconds=settings.human_handoff_projection_lease_seconds,
            max_attempts=settings.human_handoff_projection_max_attempts,
        )

    chatwoot_worker: ChatwootWorker | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        try:
            if resolution_worker is not None:
                await resolution_worker.start()
            if durable_dispatcher is not None:
                await durable_dispatcher.start()
            if opt_out_projection_worker is not None:
                await opt_out_projection_worker.start()
            if human_handoff_projection_worker is not None:
                await human_handoff_projection_worker.start()
            if chatwoot_worker is not None:
                await chatwoot_worker.start()
            yield
        finally:
            for worker_name, worker in (
                ("chatwoot", chatwoot_worker),
                ("opt_out_projection", opt_out_projection_worker),
                ("human_handoff_projection", human_handoff_projection_worker),
                ("dispatcher", durable_dispatcher),
                ("resolution", resolution_worker),
            ):
                if worker is None:
                    continue
                try:
                    await worker.stop()
                except Exception as exc:
                    logger.warning(
                        "worker_stop_failed worker=%s error_type=%s",
                        worker_name,
                        type(exc).__name__,
                    )

    app = FastAPI(title="AI Appointment Setter Bridge", lifespan=lifespan)
    app.state.resolution_worker = resolution_worker
    app.state.durable_dispatcher = durable_dispatcher
    app.state.opt_out_projection_worker = opt_out_projection_worker
    app.state.human_handoff_projection_worker = human_handoff_projection_worker
    app.state.chatwoot_inbox = chatwoot_inbox
    app.state.chatwoot_worker = chatwoot_worker

    async def run_shadow_with_canonical_history(
        *,
        delivery_id: str,
        current_message_id: int | None,
        batch_message_ids: tuple[int, ...],
        context: dict[str, object],
    ) -> CanonicalWorkResult:
        if control_client is None or settings.agent_bot_id is None:
            if shadow_processor is not None:
                shadow_processor.record_failure(
                    delivery_id=delivery_id,
                    reason="chatwoot_history_not_configured",
                )
            return CanonicalWorkResult(proposal=None)
        conversation_id = int(str(context["conversation_ref"]))
        try:
            if opt_out_enforcement_enabled:
                assert settings.chatwoot_inbox_id is not None
                await control_client.validate_conversation_authority(
                    conversation_id=conversation_id,
                    expected_inbox_id=settings.chatwoot_inbox_id,
                )
            history = await control_client.get_conversation_messages(
                conversation_id=conversation_id,
                limit=max(200, len(set(batch_message_ids)) + 19),
                required_message_ids=batch_message_ids,
            )
        except ChatwootHistoryScanLimitError as exc:
            raise RuntimeError("chatwoot_history_scan_limit_exceeded") from exc
        except (httpx.HTTPError, ChatwootProtocolError) as exc:
            raise RetryableChatwootWorkError(
                "chatwoot_canonical_history_unavailable"
            ) from exc
        normalized = _normalize_chatwoot_history(
            history,
            agent_bot_id=settings.agent_bot_id,
        )
        current_index = next(
            (
                index
                for index, message in enumerate(normalized)
                if current_message_id is not None
                and message.get("_message_ref") == str(current_message_id)
            ),
            None,
        )
        if current_index is None:
            raise CanonicalHistoryIncompleteError(
                "current_message_not_in_canonical_history"
            )
        normalized = normalized[: current_index + 1]
        message_indexes = {
            message.get("_message_ref"): index
            for index, message in enumerate(normalized)
        }
        expected_refs = {str(message_id) for message_id in batch_message_ids}
        if not expected_refs.issubset(message_indexes):
            raise CanonicalHistoryIncompleteError(
                "batched_messages_not_in_canonical_history"
            )
        external_user_id = (
            settings.allowed_jid.split("@", 1)[0]
            if settings.allowed_jid is not None
            else ""
        )
        if not external_user_id.isdigit():
            raise CanonicalHistoryIncompleteError(
                "canonical_external_user_id_invalid"
            )
        if opt_out_enforcement_enabled:
            assert shared_supabase is not None
            assert settings.chatwoot_account_id is not None
            assert settings.chatwoot_inbox_id is not None
            try:
                stopped = await shared_supabase.has_chatwoot_opt_out_stop(
                    chatwoot_account_id=settings.chatwoot_account_id,
                    chatwoot_inbox_id=settings.chatwoot_inbox_id,
                    chatwoot_conversation_id=conversation_id,
                    external_user_id=external_user_id,
                )
            except SupabaseError as exc:
                raise RetryableChatwootWorkError(
                    "chatwoot_opt_out_stop_check_failed"
                ) from exc
            if stopped:
                try:
                    reconciliation = (
                        await shared_supabase.reconcile_chatwoot_opt_out_stop(
                            chatwoot_account_id=settings.chatwoot_account_id,
                            chatwoot_inbox_id=settings.chatwoot_inbox_id,
                            chatwoot_conversation_id=conversation_id,
                            external_user_id=external_user_id,
                        )
                    )
                except SupabaseError as exc:
                    raise RetryableChatwootWorkError(
                        "chatwoot_opt_out_reconciliation_failed"
                    ) from exc
                logger.info(
                    "chatwoot_opt_out_reconciled outcome=%s event_id=%s",
                    reconciliation.outcome,
                    reconciliation.opt_out_event_id,
                )
                return CanonicalWorkResult(proposal=None, stopped=True)

        if settings.chatwoot_durable_opt_out_enabled:
            assert shared_supabase is not None
            assert settings.chatwoot_account_id is not None
            assert settings.chatwoot_inbox_id is not None
            batch_messages = [
                message
                for message in normalized
                if message.get("_message_ref") in expected_refs
                and message.get("actor") == "prospect"
            ]
            opt_out_match = detect_explicit_opt_out(
                [message["text"] for message in batch_messages]
            )
            if opt_out_match is not None:
                matched_message = batch_messages[opt_out_match.message_index]
                message_ref = matched_message.get("_message_ref")
                created_at = matched_message.get("_created_at")
                if (
                    message_ref is None
                    or created_at is None
                    or not external_user_id.isdigit()
                ):
                    raise CanonicalHistoryIncompleteError(
                        "canonical_opt_out_evidence_incomplete"
                    )
                occurred_at = datetime.fromtimestamp(
                    int(created_at), tz=UTC
                ).isoformat()
                try:
                    result = await shared_supabase.apply_chatwoot_inbound_opt_out(
                        chatwoot_account_id=settings.chatwoot_account_id,
                        chatwoot_inbox_id=settings.chatwoot_inbox_id,
                        chatwoot_conversation_id=conversation_id,
                        chatwoot_message_id=int(message_ref),
                        external_user_id=external_user_id,
                        occurred_at=occurred_at,
                        rule_key=opt_out_match.rule_key,
                    )
                except SupabaseError as exc:
                    raise RetryableChatwootWorkError(
                        "chatwoot_opt_out_apply_failed"
                    ) from exc
                logger.info(
                    "chatwoot_opt_out_stopped outcome=%s event_id=%s",
                    result.outcome,
                    result.opt_out_event_id,
                )
                return CanonicalWorkResult(proposal=None, stopped=True)
        first_batch_index = min(
            (message_indexes[message_ref] for message_ref in expected_refs),
            default=current_index,
        )
        normalized = normalized[max(0, first_batch_index - 19) :]
        public_messages = [
            {
                "actor": message["actor"],
                "text": message["text"],
            }
            for message in normalized
        ]
        enriched_context = {**context, "messages": public_messages}
        if shadow_processor is None:
            return CanonicalWorkResult(proposal=None)
        if shadow_processor.has_result(delivery_id=delivery_id):
            return CanonicalWorkResult(
                proposal=shadow_processor.get_completed_proposal(
                    delivery_id=delivery_id
                )
            )
        await shadow_processor.run(
            delivery_id=delivery_id,
            context=enriched_context,
        )
        return CanonicalWorkResult(
            proposal=shadow_processor.get_completed_proposal(delivery_id=delivery_id)
        )

    def classify_scoped_chatwoot_event(
        payload: dict[str, object],
    ) -> EventDecision:
        if (
            settings.chatwoot_account_id is None
            and settings.chatwoot_inbox_id is None
        ):
            return classify_chatwoot_event(
                payload,
                allowed_jid=settings.allowed_jid,
                agent_bot_id=settings.agent_bot_id,
            )
        return classify_chatwoot_event(
            payload,
            allowed_jid=settings.allowed_jid,
            agent_bot_id=settings.agent_bot_id,
            expected_account_id=settings.chatwoot_account_id,
            expected_inbox_id=settings.chatwoot_inbox_id,
        )

    async def process_chatwoot_work(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        decision = classify_scoped_chatwoot_event(payload)
        if decision.action == "pause_automation":
            conversation = payload.get("conversation")
            conversation_id = (
                conversation.get("id") if isinstance(conversation, dict) else None
            )
            if (
                control_client is None
                or not isinstance(conversation_id, int)
                or isinstance(conversation_id, bool)
            ):
                raise RuntimeError("chatwoot_pause_not_configured")
            await control_client.ensure_conversation_label(
                conversation_id=conversation_id,
                label="automation_paused",
            )
            return
        if decision.reason == "invalid_message_id":
            raise RuntimeError("chatwoot_invalid_message_id")
        if not decision.accepted:
            return
        context = _shadow_context(payload)
        if context is None:
            return
        completed_proposal = (
            shadow_processor.get_completed_proposal(delivery_id=delivery_id)
            if shadow_processor is not None
            else None
        )
        must_scan_canonical_history = opt_out_enforcement_enabled or (
            shadow_processor is not None
            and not shadow_processor.has_result(delivery_id=delivery_id)
        )
        if must_scan_canonical_history:
            message_id = payload.get("id")
            canonical_result = await run_shadow_with_canonical_history(
                delivery_id=delivery_id,
                current_message_id=(
                    message_id
                    if isinstance(message_id, int) and not isinstance(message_id, bool)
                    else None
                ),
                batch_message_ids=batch_message_ids,
                context=context,
            )
            if canonical_result.stopped:
                return
            completed_proposal = canonical_result.proposal
        if not settings.automated_replies_enabled or completed_proposal is None:
            return

        message_id = payload.get("id")
        conversation = payload.get("conversation")
        conversation_id = (
            conversation.get("id") if isinstance(conversation, dict) else None
        )
        reply = completed_proposal.get("reply")
        if (
            control_client is None
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or not isinstance(conversation_id, int)
            or isinstance(conversation_id, bool)
            or not isinstance(reply, str)
        ):
            raise RuntimeError("chatwoot_reply_not_configured")
        parts = (reply,)
        try:
            persisted_parts = await reply_manifest_reader.load_existing(
                conversation_id=conversation_id,
                trigger_message_id=message_id,
                reply=reply,
            )
        except ReplySplitManifestConflictError as exc:
            raise RuntimeError("reply_split_manifest_conflict") from exc
        except ReplySplitManifestStorageError as exc:
            raise RuntimeError("reply_split_manifest_storage_error") from exc
        if persisted_parts is not None:
            parts = persisted_parts
        elif settings.reply_splitter_enabled:
            if configured_reply_splitter is None:
                raise RuntimeError("reply_splitter_not_configured")
            split_failure: str | None = None
            try:
                candidate_parts = await configured_reply_splitter.split(
                    conversation_id=conversation_id,
                    trigger_message_id=message_id,
                    reply=reply,
                )
            except ReplySplitManifestConflictError as exc:
                raise RuntimeError("reply_split_manifest_conflict") from exc
            except ReplySplitManifestStorageError as exc:
                raise RuntimeError("reply_split_manifest_storage_error") from exc
            except Exception:
                candidate_parts = (reply,)
                split_failure = "splitter_error"
            validated_parts = validate_reply_parts(reply, candidate_parts)
            if validated_parts is None:
                validated_parts = (reply,)
                split_failure = "invalid_parts"
            try:
                parts = await reply_manifest_reader.persist_parts(
                    conversation_id=conversation_id,
                    trigger_message_id=message_id,
                    reply=reply,
                    parts=validated_parts,
                    failure=split_failure,
                )
            except ReplySplitManifestConflictError as exc:
                raise RuntimeError("reply_split_manifest_conflict") from exc
            except ReplySplitManifestStorageError as exc:
                raise RuntimeError("reply_split_manifest_storage_error") from exc

        try:
            for offset, part in enumerate(parts):
                if offset > 0:
                    await reply_part_sleep(settings.reply_part_delay_seconds)
                if len(parts) == 1:
                    reply_result = await control_client.send_agent_bot_reply(
                        conversation_id=conversation_id,
                        trigger_message_id=message_id,
                        delivery_id=delivery_id,
                        content=part,
                    )
                else:
                    reply_result = await control_client.send_agent_bot_reply(
                        conversation_id=conversation_id,
                        trigger_message_id=message_id,
                        delivery_id=delivery_id,
                        content=part,
                        part_index=offset + 1,
                        part_count=len(parts),
                        prior_parts=parts[:offset],
                    )
                reply_status = reply_result.get("status")
                if reply_status == "blocked":
                    return
                if reply_status not in {"sent", "duplicate"}:
                    raise RuntimeError("invalid_chatwoot_reply_result")
        except ChatwootReplyDeliveryUnknownError as exc:
            raise RetryableChatwootWorkError("reply_delivery_unknown") from exc

    if chatwoot_inbox is not None:
        def inbound_debounce_key(payload: dict[str, object]) -> str | None:
            decision = classify_scoped_chatwoot_event(payload)
            if not decision.accepted:
                return None
            conversation = payload.get("conversation")
            conversation_id = (
                conversation.get("id") if isinstance(conversation, dict) else None
            )
            if not isinstance(conversation_id, int) or isinstance(
                conversation_id, bool
            ):
                return None
            return str(conversation_id)

        chatwoot_worker = ChatwootWorker(
            inbox=chatwoot_inbox,
            handler=process_chatwoot_work,
            debounce_key=inbound_debounce_key,
            debounce_seconds=settings.chatwoot_inbound_debounce_seconds,
        )
        app.state.chatwoot_worker = chatwoot_worker

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def readiness() -> dict[str, str]:
        handoff_readiness: dict[str, str] = {}
        if settings.human_handoff_projection_enabled:
            if shared_supabase is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="human_handoff_readiness_unavailable",
                )
            try:
                handoff_status = (
                    await shared_supabase.get_human_handoff_projection_status()
                )
            except Exception as exc:
                logger.warning(
                    "human_handoff_readiness_check_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="human_handoff_readiness_unavailable",
                ) from exc
            handoff_readiness = {
                "human_handoff_projection": "configured",
                "human_handoff_pending": str(handoff_status.pending_count),
                "human_handoff_retryable": str(handoff_status.retryable_count),
                "human_handoff_delivery_unknown": str(
                    handoff_status.delivery_unknown_count
                ),
                "human_handoff_conflicts": str(handoff_status.conflict_count),
                "human_handoff_dead_letters": str(
                    handoff_status.dead_letter_count
                ),
            }
        if pilot_boundary is None:
            return {
                "status": "ready",
                "pilot_boundary": "disabled",
                "automation_state": "default_off",
                "reason_code": "pilot_boundary_disabled",
                **handoff_readiness,
            }
        if shared_supabase is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pilot_readiness_unavailable",
            )
        try:
            pilot_status = await shared_supabase.get_pilot_runtime_status(
                pilot_boundary=pilot_boundary
            )
        except Exception as exc:
            logger.warning(
                "pilot_readiness_check_failed error_type=%s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pilot_readiness_unavailable",
            ) from exc
        if not pilot_status.configured or pilot_status.runtime_state is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=pilot_status.reason_code,
            )
        return {
            "status": "ready",
            "pilot_boundary": "configured",
            "automation_state": pilot_status.runtime_state,
            "reason_code": pilot_status.reason_code,
            **handoff_readiness,
        }

    @app.post("/webhooks/chatwoot", status_code=status.HTTP_202_ACCEPTED)
    async def receive_chatwoot_webhook(
        request: Request,
        response: Response,
        x_chatwoot_signature: str = Header(),
        x_chatwoot_timestamp: str = Header(),
        x_chatwoot_delivery: str = Header(),
    ) -> dict[str, object]:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > CHATWOOT_WEBHOOK_BODY_LIMIT_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="chatwoot_webhook_body_too_large",
                )
            body.extend(chunk)
        raw_body = bytes(body)
        if not verify_chatwoot_signature(
            raw_body=raw_body,
            timestamp=x_chatwoot_timestamp,
            received_signature=x_chatwoot_signature,
            secret=settings.webhook_secret,
        ):
            raise HTTPException(status_code=401, detail="invalid_signature")
        try:
            webhook_age = abs(time.time() - int(x_chatwoot_timestamp))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="invalid_timestamp") from exc
        if webhook_age > settings.max_age_seconds:
            raise HTTPException(status_code=401, detail="stale_webhook")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc

        decision = classify_scoped_chatwoot_event(payload)
        if decision.action == "pause_automation":
            conversation = payload.get("conversation") or {}
            conversation_id = conversation.get("id")
            if not isinstance(conversation_id, int) or isinstance(
                conversation_id, bool
            ):
                raise HTTPException(
                    status_code=422,
                    detail="invalid_conversation_id",
                )
            if control_client is None:
                raise HTTPException(
                    status_code=503,
                    detail="chatwoot_control_unavailable",
                )
            assert chatwoot_inbox is not None
            _capture_payload(
                capture_dir=settings.capture_dir,
                delivery_id=x_chatwoot_delivery,
                payload=payload,
            )
            admitted = chatwoot_inbox.admit(
                delivery_id=x_chatwoot_delivery,
                payload=payload,
            )
            if admitted:
                return {
                    "status": "accepted",
                    "delivery_id": x_chatwoot_delivery,
                }
            response.status_code = status.HTTP_200_OK
            return {
                "status": "duplicate",
                "delivery_id": x_chatwoot_delivery,
            }
        if not decision.accepted:
            logger.warning("chatwoot_webhook_ignored reason=%s", decision.reason)
            response.status_code = status.HTTP_200_OK
            return {
                "status": "ignored",
                "reason": decision.reason,
            }

        captured = _capture_payload(
            capture_dir=settings.capture_dir,
            delivery_id=x_chatwoot_delivery,
            payload=payload,
        )
        context = _shadow_context(payload)
        if (
            chatwoot_inbox is not None
            and (
                shadow_processor is not None
                or opt_out_enforcement_enabled
                or settings.chatwoot_durable_opt_out_enabled
            )
            and context is not None
        ):
            admitted = chatwoot_inbox.admit(
                delivery_id=x_chatwoot_delivery,
                payload=payload,
            )
            if admitted:
                return {
                    "status": "accepted",
                    "delivery_id": x_chatwoot_delivery,
                }
            response.status_code = status.HTTP_200_OK
            return {
                "status": "duplicate",
                "delivery_id": x_chatwoot_delivery,
            }
        if not captured:
            response.status_code = status.HTTP_200_OK
            return {
                "status": "duplicate",
                "delivery_id": x_chatwoot_delivery,
            }
        return {
            "status": "captured",
            "delivery_id": x_chatwoot_delivery,
        }

    @app.post("/webhooks/hotmart", status_code=status.HTTP_202_ACCEPTED)
    async def receive_hotmart_webhook(
        request: Request,
        response: Response,
        x_hotmart_hottok: str = Header(default=""),
    ) -> dict[str, object]:
        if settings.hotmart_hottok is None:
            raise HTTPException(
                status_code=503, detail="hotmart_not_configured"
            )
        if not verify_hotmart_token(
            received_token=x_hotmart_hottok,
            expected_token=settings.hotmart_hottok,
        ):
            raise HTTPException(status_code=401, detail="invalid_token")

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > HOTMART_WEBHOOK_BODY_LIMIT_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="hotmart_webhook_body_too_large",
                )
            body.extend(chunk)
        raw_body = bytes(body)

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="invalid_json"
            ) from exc

        decision = classify_hotmart_event(payload)
        if not decision.accepted:
            response.status_code = status.HTTP_200_OK
            return {
                "status": "ignored",
                "reason": decision.reason,
            }

        event_id = decision.event_id
        assert event_id is not None  # classify guarantees this when accepted
        event_type = decision.event_type
        assert event_type is not None

        event_obj = payload if isinstance(payload, dict) else {}
        stale = is_stale_event(
            creation_date=event_obj.get("creation_date"),
            max_age_seconds=settings.hotmart_max_age_seconds,
        )
        if stale is None:
            raise HTTPException(
                status_code=400, detail="invalid_creation_date"
            )
        if stale:
            raise HTTPException(status_code=401, detail="stale_webhook")

        if shared_supabase is None:
            raise HTTPException(
                status_code=503, detail="supabase_not_configured"
            )
        try:
            if event_type == EVENT_PURCHASE_APPROVED:
                if parse_hotmart_purchase_payload(payload) is None:
                    response.status_code = status.HTTP_200_OK
                    return {
                        "status": "ignored",
                        "reason": "invalid_purchase_payload",
                    }
                purchase_admission = (
                    await shared_supabase.admit_hotmart_purchase_approved(
                        external_event_id=event_id,
                        payload=payload,
                    )
                )
                if purchase_admission.outcome == "semantic_conflict":
                    return {
                        "status": "conflict",
                        "event_id": event_id,
                        "reason": "purchase_semantic_conflict",
                    }
                if purchase_admission.outcome == "duplicate":
                    response.status_code = status.HTTP_200_OK
                    return {
                        "status": "duplicate",
                        "event_id": event_id,
                    }
                return {
                    "status": "received",
                    "event_id": event_id,
                }
            if (
                event_type == EVENT_CART_ABANDONMENT
                and parse_hotmart_payload(payload) is None
            ):
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "ignored",
                    "reason": "invalid_cart_abandonment_payload",
                }
            abandonment_admission = (
                await shared_supabase.admit_hotmart_cart_abandonment(
                    external_event_id=event_id,
                    payload=payload,
                )
            )
            if abandonment_admission.outcome == "semantic_conflict":
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "conflict",
                    "event_id": event_id,
                    "reason": "cart_abandonment_semantic_conflict",
                }
            if abandonment_admission.outcome == "duplicate":
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "duplicate",
                    "event_id": event_id,
                }
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503, detail="webhook_persist_unavailable"
            ) from exc

        return {
            "status": "received",
            "event_id": event_id,
        }

    return app


def build_app() -> FastAPI:
    """Uvicorn application factory using environment configuration."""
    settings = Settings.from_env()
    shadow_processor: ShadowProcessor | None = None
    if settings.hermes_shadow_enabled:
        if settings.hermes_api_base_url is None or settings.hermes_api_key is None:
            raise ValueError("Hermes shadow settings are incomplete")
        shadow_processor = HermesShadowProcessor(
            base_url=settings.hermes_api_base_url,
            api_key=settings.hermes_api_key,
            model_name=settings.hermes_model_name,
            shadow_dir=settings.shadow_dir,
        )
    return create_app(settings, shadow_processor=shadow_processor)
