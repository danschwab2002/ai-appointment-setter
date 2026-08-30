"""ASGI application for the Chatwoot webhook bridge."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import tempfile
import time
import unicodedata
import uuid
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
    EVENT_PURCHASE_CANCELED,
    classify_hotmart_event,
    is_stale_event,
    parse_hotmart_purchase_payload,
    parse_hotmart_payment_failure_payload,
    parse_hotmart_payload,
    verify_hotmart_token,
)
from bridge.inbound_handoff import request_handoff_for_inbound_proposal
from bridge.lead_precheckout import parse_lead_precheckout
from bridge.messaging import (
    ChatwootMessageSender,
    MessageSender,
    WhatsAppTemplateConfig,
    allowed_phone_from_jid,
)
from bridge.opt_out import detect_explicit_opt_out
from bridge.operator_correlations import (
    InvalidCorrelationEvidence,
    build_unresolved_correlation,
)
from bridge.operator_correlation_resolutions import (
    InvalidCorrelationResolution,
    build_resolution_command,
    build_resolution_result,
    validate_confirm_resolution,
    validate_prepare_resolution,
)
from bridge.precheckout import PrecheckoutScope, parse_emulated_precheckout_submission
from bridge.recovery_agent import RecoveryAgentClient
from bridge.reply_splitter import (
    HermesReplySplitter,
    ReplySplitManifestConflictError,
    ReplySplitManifestStorageError,
    validate_reply_parts,
)
from bridge.security import verify_chatwoot_signature
from bridge.supabase import (
    OperatorCorrelationResolutionError,
    PilotBoundaryConfig,
    SupabaseClient,
    SupabaseError,
)
from bridge.worker import (
    DurableDispatcher,
    HotmartAbandonmentTimerWorker,
    HumanHandoffProjectionWorker,
    OptOutProjectionWorker,
    ResolutionWorker,
)

logger = logging.getLogger(__name__)
CHATWOOT_WEBHOOK_BODY_LIMIT_BYTES = 1024 * 1024
HOTMART_WEBHOOK_BODY_LIMIT_BYTES = 1024 * 1024
PRECHECKOUT_WEBHOOK_BODY_LIMIT_BYTES = 64 * 1024
CHATWOOT_CONVERSATION_RESET_COMMAND = "/nuevo"
CHATWOOT_CONVERSATION_RESET_CONFIRMATION = "Memoria eliminada."
PRECHECKOUT_FIRST_TOUCH_TEMPLATE_NAME = "libre_ansiedad_test_first_touch_v1"
PRECHECKOUT_FIRST_TOUCH_COPY_VERSION = "libre-ansiedad-precheckout-first-touch-v1"
JOHANNA_ABANDONMENT_TEMPLATE_NAME = "johanna_carrito_abandonado_01"
JOHANNA_ABANDONMENT_COPY_VERSION = "johanna-abandonment-one-shot-v1"
JOHANNA_PAYMENT_FAILURE_TEMPLATE_NAME = "johanna_compra_fallida_01"
JOHANNA_PAYMENT_FAILURE_COPY_VERSION = "johanna-payment-failure-one-shot-v1"
JOHANNA_ABANDONMENT_BODY_LIMIT_BYTES = 8 * 1024

_MEDICATION_GUIDANCE_SUBJECT_RE = re.compile(
    r"\b(?:medicacion|medicamento|farmaco|pastilla|antidepresiv|ansiolitic)\w*\b"
)
_MEDICATION_GUIDANCE_ACTION_RE = re.compile(
    r"\b(?:dejar|suspender|interrumpir|cambiar|reducir|aumentar|tomar|dosis|dosificacion)\w*\b"
)


def _requires_medication_guidance_handoff(content: object) -> bool:
    if not isinstance(content, str):
        return False
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", content.casefold())
        if not unicodedata.combining(character)
    )
    return bool(
        _MEDICATION_GUIDANCE_SUBJECT_RE.search(normalized)
        and _MEDICATION_GUIDANCE_ACTION_RE.search(normalized)
    )


class CanonicalHistoryIncompleteError(RetryableChatwootWorkError):
    """Raised when Chatwoot has not exposed the triggering message yet."""


@dataclass(frozen=True)
class CanonicalWorkResult:
    proposal: dict[str, object] | None
    stopped: bool = False


class ChatwootControl(Protocol):
    async def validate_conversation_authority(
        self,
        *,
        conversation_id: int,
        expected_inbox_id: int,
        expected_jid: str | None = None,
    ) -> None: ...

    async def get_conversation_messages(
        self,
        *,
        conversation_id: int,
        limit: int = 20,
        required_message_ids: tuple[int, ...] = (),
    ) -> list[dict[str, object]]: ...

    async def ensure_conversation_label(
        self,
        *,
        conversation_id: int,
        label: str,
        expected_inbox_id: int | None = None,
        expected_jid: str | None = None,
    ) -> bool: ...

    async def apply_opt_out_macro(
        self,
        *,
        conversation_id: int,
        expected_account_id: int,
        expected_inbox_id: int,
        expected_jid: str,
    ) -> None: ...

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
        expected_jid: str | None = None,
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
    allowed_jid: str | None
    capture_dir: Path
    max_age_seconds: int
    agent_bot_id: int | None = None
    chatwoot_base_url: str | None = None
    chatwoot_account_id: int | None = None
    chatwoot_control_api_access_token: str | None = None
    chatwoot_agent_bot_access_token: str | None = None
    chatwoot_pause_macro_id: int | None = None
    chatwoot_human_pause_enabled: bool = False
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
    hotmart_abandonment_timer_worker_enabled: bool = False
    hotmart_abandonment_timer_poll_interval_seconds: float = 5.0
    hotmart_abandonment_timer_batch_size: int = 10
    precheckout_form_enabled: bool = False
    precheckout_form_token: str | None = None
    precheckout_max_age_seconds: int = 300
    precheckout_test_mode_enabled: bool = False
    precheckout_test_phone_e164: str | None = None
    precheckout_tenant_ref: str = "joana"
    precheckout_funnel_ref: str = "libre-de-ansiedad"
    precheckout_landing_ref: str = "bcl-main"
    precheckout_product_ref: str = "F106691755G"
    precheckout_offer_ref: str = "bxjge6zq"
    precheckout_consent_copy_version: str = "form-screenshot-2026-08-14"
    lead_precheckout_enabled: bool = False
    lead_precheckout_secret: str | None = None
    lead_precheckout_max_age_seconds: int = 300
    lead_precheckout_site: str = "psicologajohanna"
    lead_precheckout_landing_id: str = "ads-a"
    lead_precheckout_offer_code: str = "bxjge6zq"
    precheckout_first_touch_enabled: bool = False
    precheckout_first_touch_token: str | None = None
    precheckout_delayed_first_touch_enabled: bool = False
    johanna_abandonment_one_shot_enabled: bool = False
    johanna_abandonment_one_shot_token: str | None = None
    johanna_abandonment_hotmart_auto_enabled: bool = False
    johanna_payment_failure_hotmart_enabled: bool = False
    johanna_payment_failure_outbound_enabled: bool = False
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
    chatwoot_cut_b_admission_enabled: bool = False
    chatwoot_cut_b_scope_key: str | None = None
    chatwoot_cut_b_scope_version: int | None = None
    chatwoot_cut_b_agent_enabled: bool = False
    chatwoot_scoped_inbound_senders_enabled: bool = False
    operator_correlation_read_enabled: bool = False
    operator_correlation_read_token: str | None = None
    operator_correlation_tenant_ref: str | None = None
    operator_correlation_funnel_ref: str | None = None
    operator_correlation_write_enabled: bool = False
    operator_correlation_write_token: str | None = None
    operator_correlation_actor_ref: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        shadow_enabled = os.getenv("HERMES_SHADOW_ENABLED", "false").lower() == "true"
        automated_replies_enabled = (
            os.getenv("CHATWOOT_AUTOMATED_REPLIES_ENABLED", "false").lower()
            == "true"
        )
        chatwoot_human_pause_enabled = (
            os.getenv("CHATWOOT_HUMAN_PAUSE_ENABLED", "false").lower() == "true"
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
        precheckout_form_enabled = (
            os.getenv("PRECHECKOUT_FORM_ENABLED", "false").lower() == "true"
        )
        precheckout_form_token = (
            os.getenv("PRECHECKOUT_FORM_TOKEN", "").strip() or None
        )
        precheckout_test_mode_enabled = (
            os.getenv("PRECHECKOUT_TEST_MODE_ENABLED", "false").lower() == "true"
        )
        precheckout_test_phone_e164 = (
            os.getenv("PRECHECKOUT_TEST_PHONE_E164", "").strip() or None
        )
        precheckout_first_touch_enabled = (
            os.getenv("PRECHECKOUT_FIRST_TOUCH_ENABLED", "false").lower() == "true"
        )
        precheckout_first_touch_token = (
            os.getenv("PRECHECKOUT_FIRST_TOUCH_TOKEN", "").strip() or None
        )
        precheckout_delayed_first_touch_enabled = (
            os.getenv(
                "PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED", "false"
            ).lower()
            == "true"
        )
        johanna_abandonment_one_shot_enabled = (
            os.getenv("JOHANNA_ABANDONMENT_ONE_SHOT_ENABLED", "false").lower()
            == "true"
        )
        johanna_abandonment_one_shot_token = (
            os.getenv("JOHANNA_ABANDONMENT_ONE_SHOT_TOKEN", "").strip() or None
        )
        johanna_abandonment_hotmart_auto_enabled = (
            os.getenv(
                "JOHANNA_ABANDONMENT_HOTMART_AUTO_ENABLED",
                "false",
            ).lower()
            == "true"
        )
        johanna_payment_failure_hotmart_enabled = (
            os.getenv(
                "JOHANNA_PAYMENT_FAILURE_HOTMART_ENABLED",
                "false",
            ).lower()
            == "true"
        )
        johanna_payment_failure_outbound_enabled = (
            os.getenv(
                "JOHANNA_PAYMENT_FAILURE_OUTBOUND_ENABLED",
                "false",
            ).lower()
            == "true"
        )
        lead_precheckout_enabled = (
            os.getenv("LEAD_PRECHECKOUT_ENABLED", "false").lower() == "true"
        )
        lead_precheckout_secret = (
            os.getenv("LEAD_PRECHECKOUT_SECRET", "").strip() or None
        )
        lead_precheckout_max_age_seconds = int(
            os.getenv("LEAD_PRECHECKOUT_MAX_AGE_SECONDS", "300")
        )
        lead_precheckout_site = os.getenv(
            "LEAD_PRECHECKOUT_SITE", "psicologajohanna"
        ).strip()
        lead_precheckout_landing_id = os.getenv(
            "LEAD_PRECHECKOUT_LANDING_ID", "ads-a"
        ).strip()
        lead_precheckout_offer_code = os.getenv(
            "LEAD_PRECHECKOUT_OFFER_CODE", "bxjge6zq"
        ).strip()
        allowed_jid = os.getenv("ALLOWED_WHATSAPP_JID", "").strip() or None
        allowed_phone = (
            allowed_jid.removesuffix("@s.whatsapp.net")
            if allowed_jid is not None
            else None
        )
        if precheckout_form_enabled and not precheckout_test_mode_enabled:
            raise ValueError(
                "pre-checkout provisional contract cannot be enabled from deployment env"
            )
        if precheckout_test_mode_enabled and not precheckout_form_enabled:
            raise ValueError("PRECHECKOUT_TEST_MODE_ENABLED requires PRECHECKOUT_FORM_ENABLED")
        if precheckout_form_enabled and precheckout_form_token is None:
            raise ValueError("PRECHECKOUT_FORM_TOKEN is required")
        if precheckout_first_touch_enabled and (
            not precheckout_form_enabled
            or not precheckout_test_mode_enabled
            or precheckout_first_touch_token is None
        ):
            raise ValueError(
                "PRECHECKOUT_FIRST_TOUCH_ENABLED requires test-only receiver and token"
            )
        if johanna_abandonment_one_shot_enabled and (
            johanna_abandonment_one_shot_token is None
            or len(johanna_abandonment_one_shot_token) < 32
        ):
            raise ValueError(
                "JOHANNA_ABANDONMENT_ONE_SHOT_TOKEN must contain at least 32 characters"
            )
        if precheckout_test_mode_enabled and (
            precheckout_test_phone_e164 is None
            or re.fullmatch(r"\+[1-9][0-9]{7,14}", precheckout_test_phone_e164)
            is None
        ):
            raise ValueError("PRECHECKOUT_TEST_PHONE_E164 must be canonical E.164")
        if precheckout_test_mode_enabled and (
            precheckout_test_phone_e164 != f"+{allowed_phone}"
        ):
            raise ValueError(
                "PRECHECKOUT_TEST_PHONE_E164 must match ALLOWED_WHATSAPP_JID"
            )
        precheckout_max_age_seconds = int(
            os.getenv("PRECHECKOUT_MAX_AGE_SECONDS", "300")
        )
        if precheckout_max_age_seconds < 1:
            raise ValueError("PRECHECKOUT_MAX_AGE_SECONDS must be positive")
        if lead_precheckout_enabled and lead_precheckout_secret is None:
            raise ValueError("LEAD_PRECHECKOUT_SECRET is required")
        if lead_precheckout_max_age_seconds < 1:
            raise ValueError("LEAD_PRECHECKOUT_MAX_AGE_SECONDS must be positive")
        if lead_precheckout_enabled and any(
            not value
            for value in (
                lead_precheckout_site,
                lead_precheckout_landing_id,
                lead_precheckout_offer_code,
            )
        ):
            raise ValueError("lead precheckout scope must be complete")
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
        hotmart_abandonment_timer_worker_enabled = (
            os.getenv(
                "HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED", "false"
            ).lower()
            == "true"
        )
        hotmart_abandonment_timer_poll_interval_seconds = float(
            os.getenv("HOTMART_ABANDONMENT_TIMER_POLL_INTERVAL", "5.0")
        )
        hotmart_abandonment_timer_batch_size = int(
            os.getenv("HOTMART_ABANDONMENT_TIMER_BATCH_SIZE", "10")
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
        chatwoot_cut_b_scope_version_raw = os.getenv(
            "CHATWOOT_CUT_B_SCOPE_VERSION", ""
        ).strip()

        return cls(
            webhook_secret=os.environ["CHATWOOT_WEBHOOK_SECRET"],
            allowed_jid=allowed_jid,
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
            chatwoot_human_pause_enabled=chatwoot_human_pause_enabled,
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
            hotmart_abandonment_timer_worker_enabled=(
                hotmart_abandonment_timer_worker_enabled
            ),
            hotmart_abandonment_timer_poll_interval_seconds=(
                hotmart_abandonment_timer_poll_interval_seconds
            ),
            hotmart_abandonment_timer_batch_size=(
                hotmart_abandonment_timer_batch_size
            ),
            precheckout_form_enabled=precheckout_form_enabled,
            precheckout_form_token=precheckout_form_token,
            precheckout_max_age_seconds=precheckout_max_age_seconds,
            precheckout_test_mode_enabled=precheckout_test_mode_enabled,
            precheckout_test_phone_e164=precheckout_test_phone_e164,
            lead_precheckout_enabled=lead_precheckout_enabled,
            lead_precheckout_secret=lead_precheckout_secret,
            lead_precheckout_max_age_seconds=lead_precheckout_max_age_seconds,
            lead_precheckout_site=lead_precheckout_site,
            lead_precheckout_landing_id=lead_precheckout_landing_id,
            lead_precheckout_offer_code=lead_precheckout_offer_code,
            precheckout_first_touch_enabled=precheckout_first_touch_enabled,
            precheckout_first_touch_token=precheckout_first_touch_token,
            precheckout_delayed_first_touch_enabled=(
                precheckout_delayed_first_touch_enabled
            ),
            johanna_abandonment_one_shot_enabled=(
                johanna_abandonment_one_shot_enabled
            ),
            johanna_abandonment_one_shot_token=(
                johanna_abandonment_one_shot_token
            ),
            johanna_abandonment_hotmart_auto_enabled=(
                johanna_abandonment_hotmart_auto_enabled
            ),
            johanna_payment_failure_hotmart_enabled=(
                johanna_payment_failure_hotmart_enabled
            ),
            johanna_payment_failure_outbound_enabled=(
                johanna_payment_failure_outbound_enabled
            ),
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
            chatwoot_cut_b_admission_enabled=(
                os.getenv("CHATWOOT_CUT_B_ADMISSION_ENABLED", "false").lower()
                == "true"
            ),
            chatwoot_cut_b_scope_key=(
                os.getenv("CHATWOOT_CUT_B_SCOPE_KEY", "").strip() or None
            ),
            chatwoot_cut_b_scope_version=(
                int(chatwoot_cut_b_scope_version_raw)
                if chatwoot_cut_b_scope_version_raw
                else None
            ),
            chatwoot_cut_b_agent_enabled=(
                os.getenv("CHATWOOT_CUT_B_AGENT_ENABLED", "false").lower()
                == "true"
            ),
            chatwoot_scoped_inbound_senders_enabled=(
                os.getenv(
                    "CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED", "false"
                ).lower()
                == "true"
            ),
            operator_correlation_read_enabled=(
                os.getenv("OPERATOR_CORRELATION_READ_ENABLED", "false").lower()
                == "true"
            ),
            operator_correlation_read_token=(
                os.getenv("OPERATOR_CORRELATION_READ_TOKEN", "").strip() or None
            ),
            operator_correlation_tenant_ref=(
                os.getenv("OPERATOR_CORRELATION_TENANT_REF", "").strip() or None
            ),
            operator_correlation_funnel_ref=(
                os.getenv("OPERATOR_CORRELATION_FUNNEL_REF", "").strip() or None
            ),
            operator_correlation_write_enabled=(
                os.getenv("OPERATOR_CORRELATION_WRITE_ENABLED", "false").lower()
                == "true"
            ),
            operator_correlation_write_token=(
                os.getenv("OPERATOR_CORRELATION_WRITE_TOKEN", "").strip() or None
            ),
            operator_correlation_actor_ref=(
                os.getenv("OPERATOR_CORRELATION_ACTOR_REF", "").strip() or None
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
            if actor == "prospect" and content == CHATWOOT_CONVERSATION_RESET_COMMAND:
                normalized_message["_conversation_reset"] = "true"
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


def _is_conversation_reset_message(payload: dict[str, object]) -> bool:
    return payload.get("content") == CHATWOOT_CONVERSATION_RESET_COMMAND


def _history_after_latest_reset(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    reset_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("actor") == "prospect"
            and messages[index].get("_conversation_reset") == "true"
        ),
        None,
    )
    if reset_index is None:
        return messages
    reset_history = messages[reset_index + 1 :]
    if (
        reset_history
        and reset_history[0].get("actor") == "assistant"
        and reset_history[0].get("text")
        == CHATWOOT_CONVERSATION_RESET_CONFIRMATION
    ):
        return reset_history[1:]
    return reset_history


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
    if settings.chatwoot_scoped_inbound_senders_enabled and (
        settings.chatwoot_account_id != 1
        or settings.chatwoot_inbox_id != 9
        or settings.chatwoot_cut_b_scope_key != "libre-de-ansiedad-inbound"
        or settings.chatwoot_cut_b_scope_version != 2
    ):
        raise ValueError(
            "CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED requires the exact Johanna "
            "inbound scope"
        )
    if settings.chatwoot_scoped_inbound_senders_enabled and not all((
        settings.chatwoot_cut_b_admission_enabled,
        settings.chatwoot_cut_b_agent_enabled,
        settings.automated_replies_enabled,
        settings.chatwoot_durable_opt_out_enabled,
        settings.chatwoot_human_pause_enabled,
        settings.human_handoff_admission_enabled,
        settings.human_handoff_projection_enabled,
    )):
        raise ValueError(
            "CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED requires all stop and "
            "handoff gates"
        )
    if (
        settings.johanna_abandonment_one_shot_enabled
        and settings.johanna_abandonment_hotmart_auto_enabled
    ):
        raise ValueError(
            "Johanna manual one-shot and Hotmart auto-trigger are mutually exclusive"
        )
    if settings.johanna_abandonment_one_shot_enabled and (
        settings.johanna_abandonment_one_shot_token is None
        or len(settings.johanna_abandonment_one_shot_token) < 32
    ):
        raise ValueError(
            "JOHANNA_ABANDONMENT_ONE_SHOT_TOKEN must contain at least 32 characters"
        )
    if settings.operator_correlation_read_enabled and (
        settings.operator_correlation_read_token is None
        or len(settings.operator_correlation_read_token) < 32
    ):
        raise ValueError(
            "OPERATOR_CORRELATION_READ_TOKEN must contain at least 32 characters"
        )
    if settings.operator_correlation_read_enabled and (
        settings.operator_correlation_tenant_ref is None
        or settings.operator_correlation_funnel_ref is None
    ):
        raise ValueError(
            "operator correlation tenant and funnel scope must be configured"
        )
    if settings.operator_correlation_write_enabled and (
        not settings.operator_correlation_read_enabled
        or settings.operator_correlation_write_token is None
        or len(settings.operator_correlation_write_token) < 32
        or settings.operator_correlation_actor_ref is None
        or re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{1,63}",
            settings.operator_correlation_actor_ref,
        )
        is None
    ):
        raise ValueError(
            "operator correlation writes require reads, a write token, and actor ref"
        )
    if settings.operator_correlation_write_enabled and hmac.compare_digest(
        settings.operator_correlation_write_token or "",
        settings.operator_correlation_read_token or "",
    ):
        raise ValueError("operator correlation read and write tokens must differ")
    if settings.lead_precheckout_enabled and settings.lead_precheckout_secret is None:
        raise ValueError("LEAD_PRECHECKOUT_SECRET is required")
    if settings.lead_precheckout_max_age_seconds < 1:
        raise ValueError("LEAD_PRECHECKOUT_MAX_AGE_SECONDS must be positive")
    if settings.lead_precheckout_enabled and any(
        not value
        for value in (
            settings.lead_precheckout_site,
            settings.lead_precheckout_landing_id,
            settings.lead_precheckout_offer_code,
        )
    ):
        raise ValueError("lead precheckout scope must be complete")
    if settings.lead_precheckout_enabled and (
        settings.lead_precheckout_site,
        settings.lead_precheckout_landing_id,
        settings.lead_precheckout_offer_code,
    ) != ("psicologajohanna", "ads-a", "bxjge6zq"):
        raise ValueError("lead precheckout scope is fixed for the initial pilot")
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
        or settings.johanna_abandonment_one_shot_enabled
        or settings.johanna_abandonment_hotmart_auto_enabled
    ) and settings.pilot_channel_provider == "waba":
        template_fields = (
            (settings.waba_first_touch_template_name, "WABA_FIRST_TOUCH_TEMPLATE_NAME"),
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
            first_touch_parameter="buyer_name_and_product",
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
    if settings.chatwoot_cut_b_admission_enabled and (
        settings.chatwoot_account_id is None
        or settings.chatwoot_account_id < 1
        or settings.chatwoot_inbox_id is None
        or settings.chatwoot_inbox_id < 1
        or settings.chatwoot_cut_b_scope_key is None
        or re.fullmatch(
            r"[a-z0-9_-]{1,100}",
            settings.chatwoot_cut_b_scope_key,
        )
        is None
        or settings.chatwoot_cut_b_scope_version is None
        or settings.chatwoot_cut_b_scope_version < 1
        or (
            not settings.chatwoot_scoped_inbound_senders_enabled
            and re.fullmatch(
                r"[1-9][0-9]{6,14}@s\.whatsapp\.net",
                settings.allowed_jid or "",
            )
            is None
        )
        ):
        raise ValueError(
            "CHATWOOT_CUT_B_ADMISSION_ENABLED requires canonical Chatwoot IDs "
            "and scope"
        )
    if settings.chatwoot_cut_b_agent_enabled and (
        not settings.chatwoot_cut_b_admission_enabled
        or not settings.automated_replies_enabled
    ):
        raise ValueError(
            "CHATWOOT_CUT_B_AGENT_ENABLED requires Cut B admission and "
            "automated replies"
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
        if (
            shadow_processor is not None
            or control_client is not None
            or settings.chatwoot_cut_b_admission_enabled
        )
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
    if (
        settings.operator_correlation_read_enabled
        or settings.operator_correlation_write_enabled
    ) and shared_supabase is None:
        raise ValueError("operator correlation access requires Supabase")
    first_touch_sender = message_sender
    if settings.precheckout_first_touch_enabled:
        canonical_phone = allowed_phone_from_jid(settings.allowed_jid)
        if (
            shared_supabase is None
            or settings.precheckout_first_touch_token is None
            or not settings.precheckout_form_enabled
            or not settings.precheckout_test_mode_enabled
            or settings.precheckout_test_phone_e164 != (
                f"+{canonical_phone}" if canonical_phone is not None else None
            )
            or settings.pilot_channel_provider != "waba"
            or settings.chatwoot_account_id is None
            or settings.chatwoot_inbox_id is None
            or canonical_phone is None
        ):
            raise ValueError(
                "precheckout first touch requires Supabase, WABA, inbox, token, and canonical JID"
            )
        if first_touch_sender is None:
            if not isinstance(control_client, ChatwootClient):
                raise ValueError("precheckout first touch requires Chatwoot control")
            assert settings.allowed_jid is not None
            first_touch_sender = ChatwootMessageSender(
                chatwoot=control_client,
                inbox_id=settings.chatwoot_inbox_id,
                allowed_jid=settings.allowed_jid,
                template=WhatsAppTemplateConfig(
                    first_touch_name=PRECHECKOUT_FIRST_TOUCH_TEMPLATE_NAME,
                    followup_name=PRECHECKOUT_FIRST_TOUCH_TEMPLATE_NAME,
                    language="es_AR",
                    category="MARKETING",
                    first_touch_parameter="buyer_name",
                ),
            )
    delayed_precheckout_sender = message_sender
    delayed_precheckout_sender_factory = None
    if settings.precheckout_delayed_first_touch_enabled:
        if not settings.hotmart_abandonment_timer_worker_enabled:
            raise ValueError(
                "PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED requires "
                "HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED"
            )
        if (
            shared_supabase is None
            or settings.pilot_channel_provider != "waba"
            or settings.chatwoot_account_id != 1
            or settings.chatwoot_inbox_id != 9
        ):
            raise ValueError(
                "delayed precheckout first touch requires exact Supabase, "
                "WABA, account, and inbox"
            )
        if delayed_precheckout_sender is None:
            if not isinstance(control_client, ChatwootClient):
                raise ValueError(
                    "delayed precheckout first touch requires Chatwoot control"
                )
            delayed_control_client = control_client
            delayed_inbox_id = settings.chatwoot_inbox_id

            def build_delayed_precheckout_sender(
                target_phone: str,
            ) -> ChatwootMessageSender:
                return ChatwootMessageSender(
                    chatwoot=delayed_control_client,
                    inbox_id=delayed_inbox_id,
                    allowed_jid=f"{target_phone}@s.whatsapp.net",
                    template=WhatsAppTemplateConfig(
                        first_touch_name="johanna_interes_precheckout_01",
                        followup_name=None,
                        language="es_EC",
                        category="MARKETING",
                        first_touch_parameter="buyer_name",
                    ),
                )

            delayed_precheckout_sender_factory = build_delayed_precheckout_sender
    johanna_abandonment_sender = message_sender
    if (
        settings.johanna_abandonment_one_shot_enabled
        or settings.johanna_abandonment_hotmart_auto_enabled
    ):
        canonical_phone = allowed_phone_from_jid(settings.allowed_jid)
        expected_scope_version = (
            2 if settings.johanna_abandonment_hotmart_auto_enabled else 1
        )
        johanna_boundary = (
            settings.lead_precheckout_enabled,
            settings.pilot_scope_key,
            settings.pilot_scope_version,
            settings.pilot_tenant_key,
            settings.pilot_channel_provider,
            settings.pilot_channel_account_ref,
            settings.chatwoot_account_id,
            settings.chatwoot_inbox_id,
            settings.waba_first_touch_template_name,
            settings.waba_followup_template_name,
            settings.waba_template_language,
            settings.waba_template_category,
        )
        if (
            shared_supabase is None
            or (
                settings.johanna_abandonment_one_shot_enabled
                and settings.johanna_abandonment_one_shot_token is None
            )
            or (
                settings.johanna_abandonment_hotmart_auto_enabled
                and settings.hotmart_hottok is None
            )
            or (
                settings.johanna_abandonment_one_shot_enabled
                and canonical_phone is None
            )
            or johanna_boundary
            != (
                True,
                "johanna-abandonment-template-e2e",
                expected_scope_version,
                "psicologajohanna",
                "waba",
                "chatwoot-inbox:9",
                1,
                9,
                JOHANNA_ABANDONMENT_TEMPLATE_NAME,
                None,
                "es_EC",
                "MARKETING",
            )
            or waba_template is None
        ):
            raise ValueError(
                "Johanna abandonment one-shot requires exact V1.1 scope and template"
            )
        if (
            settings.johanna_abandonment_one_shot_enabled
            and johanna_abandonment_sender is None
        ):
            if not isinstance(control_client, ChatwootClient):
                raise ValueError(
                    "Johanna abandonment one-shot requires Chatwoot control"
                )
            assert settings.chatwoot_inbox_id is not None
            assert settings.allowed_jid is not None
            johanna_abandonment_sender = ChatwootMessageSender(
                chatwoot=control_client,
                inbox_id=settings.chatwoot_inbox_id,
                allowed_jid=settings.allowed_jid,
                template=waba_template,
            )
    if settings.johanna_payment_failure_outbound_enabled:
        if (
            not settings.johanna_payment_failure_hotmart_enabled
            or shared_supabase is None
            or (message_sender is None and control_client is None)
            or settings.chatwoot_account_id != 1
            or settings.chatwoot_inbox_id != 9
            or settings.pilot_channel_provider != "waba"
            or settings.pilot_channel_account_ref != "chatwoot-inbox:9"
        ):
            raise ValueError(
                "Johanna payment-failure outbound requires exact WABA scope and admission"
            )
    if settings.chatwoot_durable_opt_out_enabled and (
        shared_supabase is None or control_client is None
    ):
        raise ValueError(
            "CHATWOOT_DURABLE_OPT_OUT_ENABLED requires Supabase and Chatwoot control"
        )
    if settings.chatwoot_cut_b_admission_enabled and shared_supabase is None:
        raise ValueError("CHATWOOT_CUT_B_ADMISSION_ENABLED requires Supabase")
    if settings.chatwoot_cut_b_agent_enabled and (
        shadow_processor is None
        or control_client is None
        or settings.agent_bot_id is None
        or settings.agent_bot_id < 1
    ):
        raise ValueError(
            "CHATWOOT_CUT_B_AGENT_ENABLED requires Hermes and Chatwoot control"
        )
    if settings.human_handoff_admission_enabled:
        inbound_handoff_enabled = (
            settings.chatwoot_cut_b_admission_enabled
            and settings.chatwoot_cut_b_agent_enabled
        )
        dispatcher_handoff_enabled = (
            settings.dispatcher_enabled
            and settings.dispatcher_outbound_enabled
            and settings.pilot_boundary_enabled
        )
        if not settings.human_handoff_projection_enabled or not (
            inbound_handoff_enabled or dispatcher_handoff_enabled
        ):
            raise ValueError(
                "HUMAN_HANDOFF_ADMISSION_ENABLED requires Cut B agent or outbound "
                "dispatcher, plus handoff projection"
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
        if shared_supabase is None or control_client is None:
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
    hotmart_abandonment_timer_worker: HotmartAbandonmentTimerWorker | None = None
    durable_dispatcher: DurableDispatcher | None = None
    if settings.hotmart_abandonment_timer_worker_enabled:
        if shared_supabase is None:
            raise ValueError(
                "Supabase is required when "
                "HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED=true"
            )
        if (
            not math.isfinite(
                settings.hotmart_abandonment_timer_poll_interval_seconds
            )
            or settings.hotmart_abandonment_timer_poll_interval_seconds <= 0
        ):
            raise ValueError(
                "HOTMART_ABANDONMENT_TIMER_POLL_INTERVAL must be positive"
            )
        if not 1 <= settings.hotmart_abandonment_timer_batch_size <= 100:
            raise ValueError(
                "HOTMART_ABANDONMENT_TIMER_BATCH_SIZE must be between 1 and 100"
            )
        hotmart_abandonment_timer_worker = HotmartAbandonmentTimerWorker(
            supabase=shared_supabase,
            poll_interval_seconds=(
                settings.hotmart_abandonment_timer_poll_interval_seconds
            ),
            batch_size=settings.hotmart_abandonment_timer_batch_size,
            message_sender=delayed_precheckout_sender,
            precheckout_sender_factory=delayed_precheckout_sender_factory,
            precheckout_first_touch_enabled=(
                settings.precheckout_delayed_first_touch_enabled
            ),
        )
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
                and settings.allowed_jid is not None
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
        assert control_client is not None
        assert settings.human_handoff_projection_worker_id is not None
        human_handoff_projection_worker = HumanHandoffProjectionWorker(
            supabase=shared_supabase,
            chatwoot=control_client,  # type: ignore[arg-type]
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
            if hotmart_abandonment_timer_worker is not None:
                await hotmart_abandonment_timer_worker.start()
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
                ("hotmart_abandonment_timer", hotmart_abandonment_timer_worker),
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
    app.state.hotmart_abandonment_timer_worker = hotmart_abandonment_timer_worker
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
        expected_jid: str | None = None,
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
                    **(
                        {"expected_jid": expected_jid}
                        if expected_jid is not None
                        else {}
                    ),
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
        sender_jid = expected_jid or settings.allowed_jid
        external_user_id = sender_jid.split("@", 1)[0] if sender_jid else ""
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
        normalized = _history_after_latest_reset(normalized)
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
            allow_any_scoped_sender=(
                settings.chatwoot_scoped_inbound_senders_enabled
            ),
        )

    async def process_chatwoot_work(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        decision = classify_scoped_chatwoot_event(payload)
        if decision.action == "pause_automation":
            if not settings.chatwoot_human_pause_enabled:
                return
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
                expected_inbox_id=(
                    settings.chatwoot_inbox_id
                    if settings.chatwoot_scoped_inbound_senders_enabled
                    else None
                ),
                expected_jid=(
                    decision.sender_jid
                    if settings.chatwoot_scoped_inbound_senders_enabled
                    else None
                ),
            )
            return
        if decision.reason == "invalid_message_id":
            raise RuntimeError("chatwoot_invalid_message_id")
        if not decision.accepted:
            return
        scoped_expected_jid = (
            decision.sender_jid
            if settings.chatwoot_scoped_inbound_senders_enabled
            else None
        )
        durable_reply_authorizer: Callable[[], Awaitable[bool]] | None = None

        async def send_scoped_agent_bot_reply(
            *,
            conversation_id: int,
            trigger_message_id: int,
            content: str,
            part_index: int = 1,
            part_count: int = 1,
            prior_parts: tuple[str, ...] = (),
        ) -> dict[str, object]:
            if control_client is None:
                raise RuntimeError("chatwoot_reply_not_configured")
            if (
                durable_reply_authorizer is not None
                and not await durable_reply_authorizer()
            ):
                return {"status": "blocked", "reason": "durable_automation_stop"}
            send_args = {
                "conversation_id": conversation_id,
                "trigger_message_id": trigger_message_id,
                "delivery_id": delivery_id,
                "content": content,
                "part_index": part_index,
                "part_count": part_count,
                "prior_parts": prior_parts,
            }
            if scoped_expected_jid is None:
                return await control_client.send_agent_bot_reply(**send_args)
            return await control_client.send_agent_bot_reply(
                **send_args,
                expected_jid=scoped_expected_jid,
            )
        if _is_conversation_reset_message(payload):
            if not settings.automated_replies_enabled:
                return
            message_id = payload.get("id")
            conversation = payload.get("conversation")
            conversation_id = (
                conversation.get("id") if isinstance(conversation, dict) else None
            )
            if (
                control_client is None
                or not isinstance(message_id, int)
                or isinstance(message_id, bool)
                or not isinstance(conversation_id, int)
                or isinstance(conversation_id, bool)
            ):
                raise RuntimeError("chatwoot_reset_reply_not_configured")
            if opt_out_enforcement_enabled:
                assert shared_supabase is not None
                assert settings.chatwoot_account_id is not None
                assert settings.chatwoot_inbox_id is not None
                reset_expected_jid = scoped_expected_jid or settings.allowed_jid
                if reset_expected_jid is None:
                    raise RuntimeError("chatwoot_reset_external_user_id_invalid")
                external_user_id = reset_expected_jid.removesuffix(
                    "@s.whatsapp.net"
                )
                if not external_user_id.isdigit():
                    raise RuntimeError("chatwoot_reset_external_user_id_invalid")
                try:
                    stopped = await shared_supabase.has_chatwoot_opt_out_stop(
                        chatwoot_account_id=settings.chatwoot_account_id,
                        chatwoot_inbox_id=settings.chatwoot_inbox_id,
                        chatwoot_conversation_id=conversation_id,
                        external_user_id=external_user_id,
                    )
                except SupabaseError as exc:
                    raise RetryableChatwootWorkError(
                        "chatwoot_reset_opt_out_stop_check_failed"
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
                            "chatwoot_reset_opt_out_reconciliation_failed"
                        ) from exc
                    logger.info(
                        "chatwoot_reset_opt_out_reconciled outcome=%s event_id=%s",
                        reconciliation.outcome,
                        reconciliation.opt_out_event_id,
                    )
                    return
            try:
                reply_result = await send_scoped_agent_bot_reply(
                    conversation_id=conversation_id,
                    trigger_message_id=message_id,
                    content=CHATWOOT_CONVERSATION_RESET_CONFIRMATION,
                )
            except ChatwootReplyDeliveryUnknownError as exc:
                raise RetryableChatwootWorkError(
                    "reset_reply_delivery_unknown"
                ) from exc
            reply_status = reply_result.get("status")
            if reply_status == "blocked":
                return
            if reply_status not in {"sent", "duplicate"}:
                raise RuntimeError("invalid_chatwoot_reset_reply_result")
            return
        admission = None
        if settings.chatwoot_cut_b_admission_enabled:
            assert shared_supabase is not None
            assert settings.chatwoot_cut_b_scope_key is not None
            assert settings.chatwoot_cut_b_scope_version is not None
            conversation = payload.get("conversation")
            conversation_id = (
                conversation.get("id") if isinstance(conversation, dict) else None
            )
            sender_jid = decision.sender_jid
            external_user_id = (
                sender_jid.removesuffix("@s.whatsapp.net")
                if isinstance(sender_jid, str)
                and sender_jid.endswith("@s.whatsapp.net")
                else ""
            )
            if (
                not isinstance(conversation_id, int)
                or isinstance(conversation_id, bool)
                or conversation_id < 1
                or not external_user_id.isdigit()
            ):
                raise RuntimeError("chatwoot_cut_b_canonical_identity_invalid")
            try:
                admission = await shared_supabase.admit_inbound_commercial_case(
                    scope_key=settings.chatwoot_cut_b_scope_key,
                    scope_version=settings.chatwoot_cut_b_scope_version,
                    external_conversation_id=conversation_id,
                    external_user_id=external_user_id,
                )
            except SupabaseError as exc:
                raise RetryableChatwootWorkError(
                    "chatwoot_cut_b_admission_failed"
                ) from exc

            async def reauthorize_durable_reply() -> bool:
                try:
                    authorization = (
                        await shared_supabase.admit_inbound_commercial_case(
                            scope_key=settings.chatwoot_cut_b_scope_key,
                            scope_version=settings.chatwoot_cut_b_scope_version,
                            external_conversation_id=conversation_id,
                            external_user_id=external_user_id,
                        )
                    )
                except SupabaseError as exc:
                    raise RetryableChatwootWorkError(
                        "chatwoot_cut_b_reauthorization_failed"
                    ) from exc
                return authorization.outcome in {"created", "already_exists"}

            durable_reply_authorizer = reauthorize_durable_reply
            logger.info(
                "chatwoot_cut_b_admitted outcome=%s",
                admission.outcome,
            )
            if (
                not settings.chatwoot_cut_b_agent_enabled
                or admission.outcome in {"evidence_conflict", "blocked"}
            ):
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
                expected_jid=scoped_expected_jid,
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
        if (
            settings.human_handoff_admission_enabled
            and completed_proposal.get("decision") != "handoff"
            and _requires_medication_guidance_handoff(payload.get("content"))
        ):
            completed_proposal = {**completed_proposal, "decision": "handoff"}
            logger.info(
                "chatwoot_inbound_handoff_forced "
                "reason=direct_medication_guidance"
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
        if settings.human_handoff_admission_enabled and (
            completed_proposal.get("decision") == "handoff"
        ):
            assert shared_supabase is not None
            assert admission is not None
            assert settings.handoff_projection_policy_key is not None
            assert settings.handoff_projection_policy_version is not None
            try:
                handoff = await request_handoff_for_inbound_proposal(
                    proposal=completed_proposal,
                    admission=admission,
                    external_conversation_id=conversation_id,
                    trigger_message_id=message_id,
                    projection_policy_key=settings.handoff_projection_policy_key,
                    projection_policy_version=(
                        settings.handoff_projection_policy_version
                    ),
                    supabase=shared_supabase,
                    now=datetime.now(UTC).isoformat(),
                )
            except SupabaseError as exc:
                raise RetryableChatwootWorkError(
                    "chatwoot_inbound_handoff_failed"
                ) from exc
            if handoff is None:
                raise RuntimeError("chatwoot_inbound_handoff_not_requested")
            logger.info(
                "chatwoot_inbound_handoff_requested outcome=%s request_id=%s",
                getattr(handoff, "outcome", "unknown"),
                getattr(handoff, "handoff_request_id", "unknown"),
            )
            try:
                await control_client.ensure_conversation_label(
                    conversation_id=conversation_id,
                    label="automation_paused",
                    expected_inbox_id=(
                        settings.chatwoot_inbox_id
                        if scoped_expected_jid is not None
                        else None
                    ),
                    expected_jid=scoped_expected_jid,
                )
            except (httpx.HTTPError, ChatwootProtocolError) as exc:
                raise RetryableChatwootWorkError(
                    "handoff_automation_pause_not_confirmed"
                ) from exc
            return
        if (
            durable_reply_authorizer is not None
            and not await durable_reply_authorizer()
        ):
            return
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
                    reply_result = await send_scoped_agent_bot_reply(
                        conversation_id=conversation_id,
                        trigger_message_id=message_id,
                        content=part,
                    )
                else:
                    reply_result = await send_scoped_agent_bot_reply(
                        conversation_id=conversation_id,
                        trigger_message_id=message_id,
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
            if _is_conversation_reset_message(payload):
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

    if settings.operator_correlation_read_enabled:
        operator_token = settings.operator_correlation_read_token
        operator_tenant = settings.operator_correlation_tenant_ref
        operator_funnel = settings.operator_correlation_funnel_ref
        assert operator_token is not None
        assert operator_tenant is not None
        assert operator_funnel is not None
        assert shared_supabase is not None

        def require_operator_token(authorization: str | None) -> None:
            expected = f"Bearer {operator_token}"
            if authorization is None or not hmac.compare_digest(
                authorization.encode("utf-8"), expected.encode("utf-8")
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="operator_authentication_required",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        @app.get("/internal/operator/correlations/unresolved")
        async def list_unresolved_correlations(
            limit: int = 20,
            authorization: str | None = Header(default=None, alias="Authorization"),
        ) -> dict[str, object]:
            require_operator_token(authorization)
            if limit < 1 or limit > 50:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="limit_out_of_range",
                )
            try:
                raw_rows = (
                    await shared_supabase.list_unresolved_purchase_intent_correlations(
                        tenant_ref=operator_tenant,
                        funnel_ref=operator_funnel,
                        limit=limit
                    )
                )
                cases = [
                    build_unresolved_correlation(row, include_candidates=False)
                    for row in raw_rows
                ]
            except (SupabaseError, InvalidCorrelationEvidence) as exc:
                logger.warning(
                    "operator_correlation_list_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="operator_correlation_read_unavailable",
                ) from exc
            return {"count": len(cases), "cases": cases}

        @app.get("/internal/operator/correlations/unresolved/{case_id}")
        async def get_unresolved_correlation(
            case_id: str,
            authorization: str | None = Header(default=None, alias="Authorization"),
        ) -> dict[str, object]:
            require_operator_token(authorization)
            try:
                normalized_case_id = str(uuid.UUID(case_id))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="invalid_case_id",
                ) from exc
            try:
                raw = await shared_supabase.get_unresolved_purchase_intent_correlation(
                    tenant_ref=operator_tenant,
                    funnel_ref=operator_funnel,
                    webhook_event_id=normalized_case_id
                )
                if raw is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="unresolved_correlation_not_found",
                    )
                case = build_unresolved_correlation(raw, include_candidates=True)
            except HTTPException:
                raise
            except (SupabaseError, InvalidCorrelationEvidence) as exc:
                logger.warning(
                    "operator_correlation_get_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="operator_correlation_read_unavailable",
                ) from exc
            return {"case": case}

    if settings.operator_correlation_write_enabled:
        operator_write_token = settings.operator_correlation_write_token
        operator_tenant = settings.operator_correlation_tenant_ref
        operator_funnel = settings.operator_correlation_funnel_ref
        operator_actor = settings.operator_correlation_actor_ref
        assert operator_write_token is not None
        assert operator_tenant is not None
        assert operator_funnel is not None
        assert operator_actor is not None
        assert shared_supabase is not None

        def require_operator_write_token(authorization: str | None) -> None:
            expected = f"Bearer {operator_write_token}"
            if authorization is None or not hmac.compare_digest(
                authorization.encode("utf-8"), expected.encode("utf-8")
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="operator_write_authentication_required",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        def operator_resolution_domain_error(
            exc: OperatorCorrelationResolutionError,
        ) -> HTTPException:
            status_by_reason = {
                "invalid_operator_correlation_resolution": (
                    status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                "operator_correlation_case_not_found": status.HTTP_404_NOT_FOUND,
                "operator_correlation_stale_evidence": status.HTTP_409_CONFLICT,
                "operator_correlation_command_expired": status.HTTP_409_CONFLICT,
                "operator_correlation_already_resolved": status.HTTP_409_CONFLICT,
                "operator_correlation_idempotency_conflict": status.HTTP_409_CONFLICT,
            }
            return HTTPException(
                status_code=status_by_reason[exc.reason],
                detail=exc.reason,
            )

        @app.post("/internal/operator/correlations/resolutions/prepare")
        async def prepare_operator_correlation_resolution(
            payload: dict[str, object],
            authorization: str | None = Header(default=None, alias="Authorization"),
        ) -> dict[str, object]:
            require_operator_write_token(authorization)
            try:
                prepared = validate_prepare_resolution(payload)
                raw = await shared_supabase.prepare_operator_correlation_resolution(
                    tenant_ref=operator_tenant,
                    funnel_ref=operator_funnel,
                    actor_ref=operator_actor,
                    idempotency_key=prepared["idempotency_key"],
                    webhook_event_id=prepared["case_id"],
                    action=prepared["action"],
                    selected_purchase_intent_id=prepared["candidate_id"],
                    verification_basis=prepared["verification_basis"],
                )
                command = build_resolution_command(raw)
            except InvalidCorrelationResolution as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="invalid_operator_correlation_resolution",
                ) from exc
            except OperatorCorrelationResolutionError as exc:
                raise operator_resolution_domain_error(exc) from exc
            except SupabaseError as exc:
                logger.warning(
                    "operator_correlation_prepare_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="operator_correlation_write_unavailable",
                ) from exc
            return {"command": command}

        @app.post("/internal/operator/correlations/resolutions/confirm")
        async def confirm_operator_correlation_resolution(
            payload: dict[str, object],
            authorization: str | None = Header(default=None, alias="Authorization"),
        ) -> dict[str, object]:
            require_operator_write_token(authorization)
            try:
                confirmation = validate_confirm_resolution(payload)
                raw = await shared_supabase.confirm_operator_correlation_resolution(
                    tenant_ref=operator_tenant,
                    funnel_ref=operator_funnel,
                    actor_ref=operator_actor,
                    command_id=confirmation["command_id"],
                    expected_action=confirmation["expected_action"],
                    expected_purchase_intent_id=confirmation[
                        "expected_candidate_id"
                    ],
                )
                resolution = build_resolution_result(raw)
            except InvalidCorrelationResolution as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="invalid_operator_correlation_resolution",
                ) from exc
            except OperatorCorrelationResolutionError as exc:
                raise operator_resolution_domain_error(exc) from exc
            except SupabaseError as exc:
                logger.warning(
                    "operator_correlation_confirm_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="operator_correlation_write_unavailable",
                ) from exc
            return {"resolution": resolution}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def readiness() -> dict[str, str]:
        precheckout_readiness: dict[str, str] = {
            "precheckout_delayed_first_touch": "disabled",
        }
        if settings.precheckout_delayed_first_touch_enabled:
            if shared_supabase is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="precheckout_delayed_readiness_unavailable",
                )
            try:
                precheckout_status = await (
                    shared_supabase.get_precheckout_delayed_first_touch_readiness()
                )
            except Exception as exc:
                logger.warning(
                    "precheckout_delayed_readiness_check_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="precheckout_delayed_readiness_unavailable",
                ) from exc
            if precheckout_status.reason_code != "precheckout_first_touch_ready":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=precheckout_status.reason_code,
                )
            if (
                not precheckout_status.migration_tracking_complete
                or not precheckout_status.scope_configured
                or precheckout_status.runtime_state != "inactive"
                or precheckout_status.runtime_generation != 0
                or not precheckout_status.timer_binding_enabled
                or not precheckout_status.first_touch_binding_enabled
            ):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="precheckout_delayed_state_mismatch",
                )
            precheckout_readiness = {
                "precheckout_delayed_first_touch": "enabled",
                "precheckout_delayed_database": precheckout_status.reason_code,
                "precheckout_delayed_due": str(precheckout_status.due_count),
                "precheckout_delayed_reserved": str(
                    precheckout_status.reserved_count
                ),
                "precheckout_delayed_request_started": str(
                    precheckout_status.request_started_count
                ),
                "precheckout_delayed_delivery_unknown": str(
                    precheckout_status.delivery_unknown_count
                ),
            }
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
                **precheckout_readiness,
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
            **precheckout_readiness,
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
            if not settings.chatwoot_human_pause_enabled:
                logger.warning(
                    "chatwoot_webhook_ignored reason=human_pause_disabled"
                )
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "ignored",
                    "reason": "human_pause_disabled",
                }
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
                or settings.chatwoot_cut_b_admission_enabled
            )
            and (
                context is not None
                or settings.chatwoot_cut_b_admission_enabled
            )
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

    @app.post("/webhooks/lead", status_code=status.HTTP_200_OK)
    async def receive_lead_precheckout_webhook(
        request: Request,
        content_type: str = Header(default=""),
        user_agent: str = Header(default=""),
        x_lancemos_event: str = Header(default=""),
        x_lancemos_delivery: str = Header(default=""),
        x_lancemos_signature: str = Header(default=""),
    ) -> dict[str, object]:
        if not settings.lead_precheckout_enabled:
            raise HTTPException(status_code=503, detail="lead_precheckout_not_enabled")
        if settings.lead_precheckout_secret is None:
            raise HTTPException(status_code=503, detail="lead_precheckout_not_configured")
        normalized_content_type = ";".join(
            part.strip().lower() for part in content_type.split(";")
        )
        if (
            normalized_content_type != "application/json;charset=utf-8"
            or user_agent != "lancemos-lead-relay/1.0"
        ):
            raise HTTPException(
                status_code=400,
                detail="invalid_lead_transport_headers",
            )

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > PRECHECKOUT_WEBHOOK_BODY_LIMIT_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="lead_precheckout_body_too_large",
                )
            body.extend(chunk)
        raw_body = bytes(body)
        expected_signature = "sha256=" + hmac.new(
            settings.lead_precheckout_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(x_lancemos_signature, expected_signature):
            raise HTTPException(status_code=401, detail="invalid_lead_signature")
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc
        submission = parse_lead_precheckout(payload)
        if submission is None:
            raise HTTPException(status_code=400, detail="invalid_lead_precheckout_payload")
        if (
            x_lancemos_event != "lead.precheckout"
            or x_lancemos_event != payload.get("event")
            or x_lancemos_delivery != submission.external_submission_id
        ):
            raise HTTPException(status_code=400, detail="lead_header_payload_mismatch")
        if (
            submission.site != settings.lead_precheckout_site
            or submission.landing_id != settings.lead_precheckout_landing_id
            or submission.offer_code != settings.lead_precheckout_offer_code
        ):
            raise HTTPException(status_code=403, detail="lead_precheckout_outside_scope")
        age_seconds = (datetime.now(UTC) - submission.submitted_at).total_seconds()
        if age_seconds < -60 or age_seconds > settings.lead_precheckout_max_age_seconds:
            raise HTTPException(status_code=401, detail="stale_lead_precheckout")
        if shared_supabase is None:
            raise HTTPException(status_code=503, detail="supabase_not_configured")
        assert isinstance(payload, dict)
        try:
            admission = await shared_supabase.admit_observed_lead_precheckout(
                external_submission_id=submission.external_submission_id,
                raw_payload=payload,
                canonical_payload=submission.as_canonical_payload(),
            )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503, detail="lead_precheckout_persist_unavailable"
            ) from exc
        response_status = {
            "inserted": "received",
            "duplicate": "duplicate",
            "semantic_conflict": "conflict",
        }[admission.outcome]
        return {
            "status": response_status,
            "delivery_id": submission.external_submission_id,
            "purchase_intent_id": admission.purchase_intent_id,
            "activation_authorized": False,
            "contact_authorized": False,
        }

    @app.post("/webhooks/precheckout", status_code=status.HTTP_202_ACCEPTED)
    async def receive_precheckout_webhook(
        request: Request,
        response: Response,
        x_precheckout_token: str = Header(default=""),
    ) -> dict[str, object]:
        if not settings.precheckout_form_enabled:
            raise HTTPException(status_code=503, detail="precheckout_not_enabled")
        if settings.precheckout_form_token is None:
            raise HTTPException(status_code=503, detail="precheckout_not_configured")
        if not hmac.compare_digest(
            x_precheckout_token.encode("utf-8"),
            settings.precheckout_form_token.encode("utf-8"),
        ):
            raise HTTPException(status_code=401, detail="invalid_token")

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > PRECHECKOUT_WEBHOOK_BODY_LIMIT_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="precheckout_webhook_body_too_large",
                )
            body.extend(chunk)
        try:
            payload = json.loads(bytes(body))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc

        submission = parse_emulated_precheckout_submission(
            payload,
            scope=PrecheckoutScope(
                tenant_ref=settings.precheckout_tenant_ref,
                funnel_ref=settings.precheckout_funnel_ref,
                landing_ref=settings.precheckout_landing_ref,
                product_ref=settings.precheckout_product_ref,
                offer_ref=settings.precheckout_offer_ref,
                consent_copy_version=settings.precheckout_consent_copy_version,
            ),
        )
        if submission is None:
            raise HTTPException(status_code=400, detail="invalid_precheckout_payload")
        if (
            not settings.precheckout_test_mode_enabled
            or settings.precheckout_test_phone_e164 is None
        ):
            raise HTTPException(status_code=503, detail="precheckout_test_mode_required")
        allowed_jid_match = re.fullmatch(
            r"([1-9][0-9]{7,14})@s\.whatsapp\.net",
            settings.allowed_jid or "",
        )
        allowed_jid_phone_e164 = (
            f"+{allowed_jid_match.group(1)}" if allowed_jid_match is not None else None
        )
        if (
            allowed_jid_phone_e164 is None
            or settings.precheckout_test_phone_e164 != allowed_jid_phone_e164
            or f"+{submission.normalized_phone}" != allowed_jid_phone_e164
        ):
            raise HTTPException(
                status_code=403,
                detail="precheckout_test_phone_not_allowed",
            )
        age_seconds = (datetime.now(UTC) - submission.submitted_at).total_seconds()
        if age_seconds < -60 or age_seconds > settings.precheckout_max_age_seconds:
            raise HTTPException(status_code=401, detail="stale_precheckout_submission")
        if shared_supabase is None:
            raise HTTPException(status_code=503, detail="supabase_not_configured")
        assert isinstance(payload, dict)
        try:
            admission = await shared_supabase.admit_precheckout_form_submission(
                external_submission_id=submission.external_submission_id,
                raw_payload=payload,
                canonical_payload=submission.as_canonical_payload(),
            )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503, detail="precheckout_persist_unavailable"
            ) from exc

        if admission.outcome == "semantic_conflict":
            response.status_code = status.HTTP_200_OK
            return {
                "status": "conflict",
                "submission_id": submission.external_submission_id,
                "purchase_intent_id": admission.purchase_intent_id,
                "activation_authorized": False,
                "test_only": True,
                "generalizable": False,
            }
        if admission.outcome == "duplicate":
            response.status_code = status.HTTP_200_OK
            return {
                "status": "duplicate",
                "submission_id": submission.external_submission_id,
                "purchase_intent_id": admission.purchase_intent_id,
                "activation_authorized": False,
                "test_only": True,
                "generalizable": False,
            }
        return {
            "status": "received",
            "submission_id": submission.external_submission_id,
            "purchase_intent_id": admission.purchase_intent_id,
            "activation_authorized": False,
            "test_only": True,
            "generalizable": False,
        }

    async def _execute_johanna_abandonment_delivery(
        *,
        command_key: str,
        purchase_intent_id: str,
        hotmart_webhook_event_id: str | None,
    ) -> tuple[int, dict[str, object]]:
        canonical_phone = allowed_phone_from_jid(settings.allowed_jid)
        expected_scope_version = 2 if hotmart_webhook_event_id is not None else 1
        expected_generation = 1 if hotmart_webhook_event_id is not None else 0
        if (
            shared_supabase is None
            or settings.chatwoot_account_id != 1
            or settings.chatwoot_inbox_id != 9
            or settings.pilot_scope_key != "johanna-abandonment-template-e2e"
            or settings.pilot_scope_version != expected_scope_version
            or (
                hotmart_webhook_event_id is None
                and (johanna_abandonment_sender is None or canonical_phone is None)
            )
            or (
                hotmart_webhook_event_id is not None
                and johanna_abandonment_sender is None
                and not isinstance(control_client, ChatwootClient)
            )
        ):
            raise HTTPException(
                status_code=503,
                detail="johanna_abandonment_one_shot_not_configured",
            )
        begin_args: dict[str, object] = {
            "command_key": command_key,
            "purchase_intent_id": purchase_intent_id,
            "chatwoot_account_id": settings.chatwoot_account_id,
            "chatwoot_inbox_id": settings.chatwoot_inbox_id,
            "scope_key": settings.pilot_scope_key,
            "scope_version": settings.pilot_scope_version,
            "expected_generation": expected_generation,
        }
        try:
            if hotmart_webhook_event_id is None:
                assert canonical_phone is not None
                started = await shared_supabase.begin_johanna_abandonment_one_shot(
                    allowed_external_user_id=canonical_phone,
                    **begin_args,  # type: ignore[arg-type]
                )
            else:
                started = (
                    await shared_supabase.begin_johanna_abandonment_hotmart_auto(
                        hotmart_webhook_event_id=hotmart_webhook_event_id,
                        **begin_args,  # type: ignore[arg-type]
                    )
                )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=409,
                detail="johanna_abandonment_one_shot_not_authorized",
            ) from exc

        expected_metadata = (
            JOHANNA_ABANDONMENT_TEMPLATE_NAME,
            "es_EC",
            "MARKETING",
            JOHANNA_ABANDONMENT_COPY_VERSION,
        )
        actual_metadata = (
            started.template_name,
            started.template_language,
            started.template_category,
            started.copy_version,
        )
        target_phone_is_canonical = (
            isinstance(started.target_phone, str)
            and re.fullmatch(r"[1-9][0-9]{7,14}", started.target_phone) is not None
        )
        if (
            actual_metadata != expected_metadata
            or not target_phone_is_canonical
            or (
                hotmart_webhook_event_id is None
                and started.target_phone != canonical_phone
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="johanna_abandonment_one_shot_metadata_mismatch",
            )
        if started.outcome == "budget_consumed":
            return 200, {
                "status": "ignored",
                "reason": "contact_budget_consumed",
                "message_count": 1,
                "followups_allowed": 0,
                "test_only": True,
                "generalizable": False,
            }
        if started.outcome == "replay":
            if started.command_status == "accepted_by_chatwoot":
                return 200, {
                    "status": "accepted_by_chatwoot",
                    "command_id": started.command_id,
                    "message_count": 1,
                    "followups_allowed": 0,
                    "test_only": True,
                    "generalizable": False,
                }
            raise HTTPException(
                status_code=409,
                detail="johanna_abandonment_one_shot_reconciliation_required",
            )

        delivery_sender = johanna_abandonment_sender
        if hotmart_webhook_event_id is not None and message_sender is None:
            if not isinstance(control_client, ChatwootClient) or waba_template is None:
                raise HTTPException(
                    status_code=503,
                    detail="johanna_abandonment_one_shot_not_configured",
                )
            delivery_sender = ChatwootMessageSender(
                chatwoot=control_client,
                inbox_id=9,
                allowed_jid=f"{started.target_phone}@s.whatsapp.net",
                template=waba_template,
            )
        if delivery_sender is None:
            raise HTTPException(
                status_code=503,
                detail="johanna_abandonment_one_shot_not_configured",
            )
        result = await delivery_sender.send_first_touch(
            phone=started.target_phone,
            buyer_name=started.buyer_name,
            buyer_email=started.buyer_email,
            product_name=started.product_name,
            content="Recuperación supervisada de carrito de Libre de Ansiedad.",
            delivery_id=started.command_id,
        )
        if (
            result.status == "sent"
            and result.conversation_id is not None
            and result.message_id is not None
        ):
            try:
                await shared_supabase.finish_johanna_abandonment_one_shot(
                    command_id=started.command_id,
                    outcome="accepted_by_chatwoot",
                    chatwoot_conversation_id=result.conversation_id,
                    chatwoot_message_id=result.message_id,
                    failure_code=None,
                )
            except SupabaseError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="johanna_abandonment_one_shot_finalization_unknown",
                ) from exc
            return 202, {
                "status": "accepted_by_chatwoot",
                "command_id": started.command_id,
                "message_count": 1,
                "followups_allowed": 0,
                "test_only": True,
                "generalizable": False,
            }

        stable_failure = (
            result.reason
            if result.reason
            in {
                "chatwoot_http_error",
                "chatwoot_protocol_error",
                "invalid_phone",
                "target_not_allowed",
                "template_parameters_missing",
            }
            else "sender_failed"
        )
        try:
            await shared_supabase.finish_johanna_abandonment_one_shot(
                command_id=started.command_id,
                outcome="delivery_unknown",
                chatwoot_conversation_id=None,
                chatwoot_message_id=None,
                failure_code=stable_failure,
            )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503,
                detail="johanna_abandonment_one_shot_finalization_unknown",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail="johanna_abandonment_one_shot_failed",
        )

    @app.post(
        "/internal/johanna/abandonment-one-shot",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def send_johanna_abandonment_one_shot(
        request: Request,
        response: Response,
        x_johanna_one_shot_token: str = Header(default=""),
    ) -> dict[str, object]:
        if not settings.johanna_abandonment_one_shot_enabled:
            raise HTTPException(
                status_code=503,
                detail="johanna_abandonment_one_shot_not_enabled",
            )
        if settings.johanna_abandonment_one_shot_token is None:
            raise HTTPException(
                status_code=503,
                detail="johanna_abandonment_one_shot_not_configured",
            )
        if not hmac.compare_digest(
            x_johanna_one_shot_token.encode(),
            settings.johanna_abandonment_one_shot_token.encode(),
        ):
            raise HTTPException(
                status_code=401,
                detail="invalid_johanna_abandonment_one_shot_token",
            )

        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > JOHANNA_ABANDONMENT_BODY_LIMIT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="johanna_abandonment_one_shot_body_too_large",
                )
            raw.extend(chunk)
        try:
            payload = json.loads(bytes(raw))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail="invalid_johanna_abandonment_one_shot_payload",
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "command_key",
            "purchase_intent_id",
        }:
            raise HTTPException(
                status_code=400,
                detail="invalid_johanna_abandonment_one_shot_payload",
            )
        command_key = payload.get("command_key")
        purchase_intent_id = payload.get("purchase_intent_id")
        if (
            not isinstance(command_key, str)
            or re.fullmatch(r"[a-z0-9:_-]{1,200}", command_key) is None
            or not isinstance(purchase_intent_id, str)
        ):
            raise HTTPException(
                status_code=400,
                detail="invalid_johanna_abandonment_one_shot_payload",
            )
        try:
            uuid.UUID(purchase_intent_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="invalid_johanna_abandonment_one_shot_payload",
            ) from exc

        result_status, result_body = await _execute_johanna_abandonment_delivery(
            command_key=command_key,
            purchase_intent_id=purchase_intent_id,
            hotmart_webhook_event_id=None,
        )
        response.status_code = result_status
        return result_body

    @app.post(
        "/internal/precheckout/test-first-touch",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def send_precheckout_test_first_touch(
        request: Request,
        response: Response,
        x_precheckout_first_touch_token: str = Header(default=""),
    ) -> dict[str, object]:
        if not settings.precheckout_first_touch_enabled:
            raise HTTPException(
                status_code=503, detail="precheckout_first_touch_not_enabled"
            )
        if settings.precheckout_first_touch_token is None:
            raise HTTPException(
                status_code=503, detail="precheckout_first_touch_not_configured"
            )
        if not hmac.compare_digest(
            x_precheckout_first_touch_token.encode(),
            settings.precheckout_first_touch_token.encode(),
        ):
            raise HTTPException(status_code=401, detail="invalid_token")
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="first_touch_body_too_large")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "command_key",
            "purchase_intent_id",
        }:
            raise HTTPException(status_code=400, detail="invalid_first_touch_request")
        command_key = payload.get("command_key")
        purchase_intent_id = payload.get("purchase_intent_id")
        if (
            not isinstance(command_key, str)
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", command_key) is None
            or not isinstance(purchase_intent_id, str)
        ):
            raise HTTPException(status_code=400, detail="invalid_first_touch_request")
        try:
            if str(uuid.UUID(purchase_intent_id)) != purchase_intent_id:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="invalid_first_touch_request"
            ) from exc
        allowed_phone = allowed_phone_from_jid(settings.allowed_jid)
        if allowed_phone is None or shared_supabase is None or first_touch_sender is None:
            raise HTTPException(
                status_code=503, detail="precheckout_first_touch_not_configured"
            )
        try:
            started = await shared_supabase.begin_precheckout_test_first_touch(
                command_key=command_key,
                purchase_intent_id=purchase_intent_id,
                allowed_external_user_id=allowed_phone,
                chatwoot_account_id=settings.chatwoot_account_id,  # type: ignore[arg-type]
                chatwoot_inbox_id=settings.chatwoot_inbox_id,  # type: ignore[arg-type]
            )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=409, detail="precheckout_first_touch_not_eligible"
            ) from exc
        if started.outcome == "replay":
            if started.command_status == "accepted_by_chatwoot":
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "accepted_by_chatwoot",
                    "command_id": started.command_id,
                    "message_count": 1,
                    "followups_allowed": 0,
                    "test_only": True,
                    "generalizable": False,
                }
            raise HTTPException(
                status_code=409,
                detail="precheckout_first_touch_reconciliation_required",
            )
        metadata_valid = (
            started.command_status == "request_started"
            and started.target_phone == allowed_phone
            and started.template_name == PRECHECKOUT_FIRST_TOUCH_TEMPLATE_NAME
            and started.template_language == "es_AR"
            and started.template_category == "MARKETING"
            and started.copy_version == PRECHECKOUT_FIRST_TOUCH_COPY_VERSION
        )
        if not metadata_valid:
            await shared_supabase.finish_precheckout_test_first_touch(
                command_id=started.command_id,
                outcome="failed",
                chatwoot_conversation_id=None,
                chatwoot_message_id=None,
                failure_code="configuration_mismatch",
            )
            raise HTTPException(
                status_code=503, detail="precheckout_first_touch_not_configured"
            )
        buyer_name = started.buyer_name.strip()
        content = (
            f"¡Hola, {buyer_name}! Te habla el equipo de Johanna. "
            "Vimos que completaste el formulario de Libre de Ansiedad. "
            "¿Te parece si avanzamos por acá?"
        )
        result = await first_touch_sender.send_first_touch_to_conversation(
            conversation_id=started.chatwoot_conversation_id,
            phone=started.target_phone,
            buyer_name=buyer_name,
            content=content,
            delivery_id=started.command_id,
        )
        if (
            result.status == "sent"
            and result.conversation_id is not None
            and result.message_id is not None
        ):
            try:
                await shared_supabase.finish_precheckout_test_first_touch(
                    command_id=started.command_id,
                    outcome="accepted_by_chatwoot",
                    chatwoot_conversation_id=result.conversation_id,
                    chatwoot_message_id=result.message_id,
                    failure_code=None,
                )
            except SupabaseError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="precheckout_first_touch_reconciliation_required",
                ) from exc
            return {
                "status": "accepted_by_chatwoot",
                "command_id": started.command_id,
                "message_count": 1,
                "followups_allowed": 0,
                "test_only": True,
                "generalizable": False,
            }
        failure_code = (result.reason or "sender_failed")[:120]
        terminal_outcome = "failed" if result.status == "blocked" else "delivery_unknown"
        await shared_supabase.finish_precheckout_test_first_touch(
            command_id=started.command_id,
            outcome=terminal_outcome,
            chatwoot_conversation_id=None,
            chatwoot_message_id=None,
            failure_code=failure_code,
        )
        raise HTTPException(status_code=502, detail="precheckout_first_touch_failed")

    async def _execute_johanna_payment_failure_delivery(
        *,
        payment_failure_case_id: str,
        retry_invalid_contact: bool = False,
    ) -> tuple[int, dict[str, object]]:
        if (
            shared_supabase is None
            or settings.chatwoot_account_id != 1
            or settings.chatwoot_inbox_id != 9
        ):
            raise HTTPException(
                status_code=503,
                detail="johanna_payment_failure_outbound_not_configured",
            )
        try:
            command_key = (
                "johanna-payment-failure-auto:"
                f"{payment_failure_case_id}"
            )
            if retry_invalid_contact:
                started = (
                    await shared_supabase.prepare_johanna_payment_failure_invalid_contact_retry(
                        command_key=command_key,
                        payment_failure_case_id=payment_failure_case_id,
                        chatwoot_account_id=settings.chatwoot_account_id,
                        chatwoot_inbox_id=settings.chatwoot_inbox_id,
                    )
                )
            else:
                started = (
                    await shared_supabase.begin_johanna_payment_failure_hotmart_auto(
                        command_key=command_key,
                        payment_failure_case_id=payment_failure_case_id,
                        chatwoot_account_id=settings.chatwoot_account_id,
                        chatwoot_inbox_id=settings.chatwoot_inbox_id,
                    )
                )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=409,
                detail="johanna_payment_failure_outbound_not_authorized",
            ) from exc

        if (
            started.template_name,
            started.template_language,
            started.template_category,
            started.copy_version,
        ) != (
            JOHANNA_PAYMENT_FAILURE_TEMPLATE_NAME,
            "es_EC",
            "MARKETING",
            JOHANNA_PAYMENT_FAILURE_COPY_VERSION,
        ):
            raise HTTPException(
                status_code=409,
                detail="johanna_payment_failure_outbound_metadata_mismatch",
            )
        if started.outcome == "budget_consumed":
            return 200, {
                "status": "ignored",
                "reason": "contact_budget_consumed",
                "message_count": 1,
                "followups_allowed": 0,
            }
        if started.outcome == "not_retryable":
            return 200, {
                "status": "delivery_unknown",
                "command_id": started.command_id,
                "message_count": 1,
                "followups_allowed": 0,
            }
        if started.outcome == "replay":
            if started.command_status == "accepted_by_chatwoot":
                return 200, {
                    "status": "accepted_by_chatwoot",
                    "command_id": started.command_id,
                    "message_count": 1,
                    "followups_allowed": 0,
                }
            if started.command_status == "delivery_unknown":
                return 200, {
                    "status": "delivery_unknown",
                    "command_id": started.command_id,
                    "message_count": 1,
                    "followups_allowed": 0,
                }
            raise HTTPException(
                status_code=409,
                detail="johanna_payment_failure_reconciliation_required",
            )

        payment_sender = message_sender
        if payment_sender is None:
            if not isinstance(control_client, ChatwootClient):
                raise HTTPException(
                    status_code=503,
                    detail="johanna_payment_failure_sender_not_configured",
                )
            payment_sender = ChatwootMessageSender(
                chatwoot=control_client,
                inbox_id=9,
                allowed_jid=f"{started.target_phone}@s.whatsapp.net",
                template=WhatsAppTemplateConfig(
                    first_touch_name=JOHANNA_PAYMENT_FAILURE_TEMPLATE_NAME,
                    followup_name=None,
                    language="es_EC",
                    category="MARKETING",
                    first_touch_parameter="buyer_name_and_product",
                ),
            )
        result = await payment_sender.send_first_touch(
            phone=started.target_phone,
            buyer_name=started.buyer_name,
            buyer_email=started.buyer_email,
            product_name=started.product_name,
            content="Recuperación de compra rechazada por falta de fondos.",
            delivery_id=started.command_id,
            require_existing_contact=retry_invalid_contact,
        )
        accepted = (
            result.status == "sent"
            and result.conversation_id is not None
            and result.message_id is not None
        )
        try:
            await shared_supabase.finish_johanna_abandonment_one_shot(
                command_id=started.command_id,
                outcome=(
                    "accepted_by_chatwoot" if accepted else "delivery_unknown"
                ),
                chatwoot_conversation_id=(
                    result.conversation_id if accepted else None
                ),
                chatwoot_message_id=result.message_id if accepted else None,
                failure_code=None if accepted else (result.reason or "sender_failed"),
            )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503,
                detail="johanna_payment_failure_finalization_unknown",
            ) from exc
        if not accepted:
            raise HTTPException(
                status_code=502,
                detail="johanna_payment_failure_outbound_failed",
            )
        return 202, {
            "status": "accepted_by_chatwoot",
            "command_id": started.command_id,
            "message_count": 1,
            "followups_allowed": 0,
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
            if event_type == EVENT_PURCHASE_CANCELED:
                if not settings.johanna_payment_failure_hotmart_enabled:
                    response.status_code = status.HTTP_200_OK
                    return {
                        "status": "ignored",
                        "reason": "payment_failure_disabled",
                    }
                parsed_failure = parse_hotmart_payment_failure_payload(payload)
                if parsed_failure is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="invalid_payment_failure_payload",
                    )
                failure_admission = await shared_supabase.admit_johanna_payment_failure(
                    external_event_id=event_id,
                    payload=payload,
                    normalized_email=parsed_failure.buyer_email,
                    normalized_phone=parsed_failure.buyer_phone,
                )
                if failure_admission.outcome == "semantic_conflict":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="payment_failure_semantic_conflict",
                    )
                if failure_admission.outcome == "duplicate":
                    if failure_admission.case_status == "outbound_accepted":
                        response.status_code = status.HTTP_200_OK
                        return {
                            "status": failure_admission.case_status,
                            "event_id": event_id,
                            "case_status": failure_admission.case_status,
                            "correlation_outcome": (
                                failure_admission.correlation_outcome
                            ),
                        }
                    if (
                        failure_admission.case_status == "delivery_unknown"
                        and settings.johanna_payment_failure_outbound_enabled
                        and failure_admission.correlation_outcome == "resolved"
                    ):
                        result_status, result_body = (
                            await _execute_johanna_payment_failure_delivery(
                                payment_failure_case_id=(
                                    failure_admission.payment_failure_case_id
                                ),
                                retry_invalid_contact=True,
                            )
                        )
                        response.status_code = result_status
                        return result_body
                    if failure_admission.case_status == "delivery_unknown":
                        response.status_code = status.HTTP_200_OK
                        return {
                            "status": failure_admission.case_status,
                            "event_id": event_id,
                            "case_status": failure_admission.case_status,
                            "correlation_outcome": (
                                failure_admission.correlation_outcome
                            ),
                        }
                    if (
                        settings.johanna_payment_failure_outbound_enabled
                        and failure_admission.correlation_outcome == "resolved"
                    ):
                        result_status, result_body = (
                            await _execute_johanna_payment_failure_delivery(
                                payment_failure_case_id=(
                                    failure_admission.payment_failure_case_id
                                )
                            )
                        )
                        response.status_code = result_status
                        return result_body
                    response.status_code = status.HTTP_200_OK
                    return {
                        "status": "duplicate",
                        "event_id": event_id,
                        "case_status": failure_admission.case_status,
                        "correlation_outcome": (
                            failure_admission.correlation_outcome
                        ),
                    }
                if (
                    settings.johanna_payment_failure_outbound_enabled
                    and failure_admission.correlation_outcome == "resolved"
                ):
                    result_status, result_body = (
                        await _execute_johanna_payment_failure_delivery(
                            payment_failure_case_id=(
                                failure_admission.payment_failure_case_id
                            )
                        )
                    )
                    response.status_code = result_status
                    return result_body
                return {
                    "status": "received",
                    "event_id": event_id,
                    "case_status": failure_admission.case_status,
                    "correlation_outcome": failure_admission.correlation_outcome,
                }
            if event_type == EVENT_PURCHASE_APPROVED:
                parsed_purchase = parse_hotmart_purchase_payload(payload)
                if parsed_purchase is None:
                    response.status_code = status.HTTP_200_OK
                    return {
                        "status": "ignored",
                        "reason": "invalid_purchase_payload",
                    }
                purchase_admission = (
                    await shared_supabase.admit_and_correlate_hotmart_purchase_approved(
                        external_event_id=event_id,
                        payload=payload,
                        normalized_email=parsed_purchase.buyer_email,
                        normalized_phone=parsed_purchase.buyer_phone,
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
            parsed_abandonment = parse_hotmart_payload(payload)
            if parsed_abandonment is None:
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "ignored",
                    "reason": "invalid_cart_abandonment_payload",
                }
            abandonment_admission = (
                await shared_supabase.admit_and_correlate_hotmart_cart_abandonment(
                    external_event_id=event_id,
                    payload=payload,
                    normalized_email=parsed_abandonment.buyer_email,
                    normalized_phone=parsed_abandonment.buyer_phone,
                )
            )
            if abandonment_admission.outcome == "semantic_conflict":
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "conflict",
                    "event_id": event_id,
                    "reason": "cart_abandonment_semantic_conflict",
                }
            if settings.johanna_abandonment_hotmart_auto_enabled:
                correlation = await shared_supabase.correlate_hotmart_purchase_intent(
                    webhook_event_id=abandonment_admission.webhook_event_id,
                )
                if (
                    correlation.outcome == "resolved"
                    and correlation.purchase_intent_id is not None
                    and correlation.candidate_count == 1
                    and not correlation.manual_handoff_required
                ):
                    result_status, result_body = (
                        await _execute_johanna_abandonment_delivery(
                            command_key=(
                                "johanna-hotmart-auto:"
                                f"{abandonment_admission.webhook_event_id}"
                            ),
                            purchase_intent_id=correlation.purchase_intent_id,
                            hotmart_webhook_event_id=(
                                abandonment_admission.webhook_event_id
                            ),
                        )
                    )
                    response.status_code = result_status
                    return result_body
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
