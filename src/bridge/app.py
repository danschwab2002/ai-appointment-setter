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
from dataclasses import dataclass, fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import AsyncGenerator, Awaitable, Callable, Protocol
from urllib.parse import urlparse

import httpx
from fastapi import (
    BackgroundTasks,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)

from bridge.audio_transcription import (
    DEFAULT_TRANSCRIPTION_MODELS,
    AudioTranscriber,
    AudioTranscriptionError,
    needs_audio_transcription,
)
from bridge.chatwoot import (
    ChatwootClient,
    ChatwootHistoryScanLimitError,
    ChatwootProtocolError,
    ChatwootReplyDeliveryUnknownError,
    StalledChatwootConversation,
    TeamMessageTimestampError,
    seconds_since_last_team_message,
)
from bridge.chatwoot_inbox import (
    ChatwootStalledConversationMonitor,
    ChatwootWorker,
    DurableChatwootInbox,
    RetryableChatwootWorkError,
)
from bridge.followup_discount import (
    COUPON_CODE_RE,
    ConversationFollowupSweeper,
)
from bridge.reactivation import ConversationReactivationSweeper
from bridge.commercial_ally import CommercialAllyConfig, JOHANNA_COMMERCIAL_ALLY
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_manifest import GHL_RISK_GATED_FLOWS, InstanceManifest
from bridge.checkout_delivery import CheckoutDeliveryError, deliver_checkout_issuance_v2
from bridge.filtering import EventDecision, classify_chatwoot_event
from bridge.agent_provenance import AgentTurn, CONTEXT_BUILDER_VERSION
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
from bridge.ghl_precheckout_adapter import (
    GhlAdapterRejection,
    ghl_body_token,
    parse_ghl_body,
    translate_ghl_form_submission,
)
from bridge.lead_precheckout import LeadPrecheckoutSubmission, parse_lead_precheckout
from bridge.messaging import (
    ChatwootMessageSender,
    FinalMetaEffectGate,
    MessageSender,
    WhatsAppTemplateConfig,
    allowed_phone_from_jid,
)
from bridge.opt_out import detect_explicit_opt_out
from bridge.phones import equivalent_whatsapp_phones, whatsapp_phone_region
from bridge.operator_correlations import (
    InvalidCorrelationEvidence,
    build_unresolved_correlation,
)
from bridge.operator_correlation_resolutions import (
    InvalidCorrelationResolution,
    build_resolution_command,
    build_resolution_result,
    resolve_actor_ref,
    validate_actor_prefix,
    validate_confirm_resolution,
    validate_prepare_resolution,
)
from bridge.payment_link import (
    PaymentLinkConfig,
    PaymentLinkUnavailable,
    build_payment_link,
    render_payment_link_reply,
)
from bridge.precheckout import PrecheckoutScope, parse_emulated_precheckout_submission
from bridge.recovery_agent import RecoveryAgentClient
from bridge.reply_splitter import (
    HermesReplySplitter,
    ReplySplitManifestConflictError,
    ReplySplitManifestStorageError,
    validate_reply_parts,
)
from bridge.lead_first_name import (
    FirstNameInferenceClient,
    infer_and_record_first_name,
    resolve_greeting_name,
)
from bridge.correlation_preresolution import (
    CorrelationPreresolutionClient,
    CorrelationPreresolutionWorker,
)
from bridge.security import verify_chatwoot_signature
from bridge.slack_handoff_projection import SlackHandoffProjectionWorker
from bridge.slack_projection import SlackCorrelationProjectionWorker
from bridge.slack_runtime import SlackBridgeRuntime, create_slack_bridge_runtime
from bridge.supabase import (
    PILOT_SCOPE_CONSENTED_AUDIENCE_MODES,
    InboundCommercialCaseAdmissionResult,
    OperatorCorrelationResolutionError,
    PilotBoundaryConfig,
    PrecheckoutAdmissionResult,
    SupabaseClient,
    SupabaseError,
    SupabasePermanentError,
    sck_carries_hermes_issuance,
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
JOHANNA_FUNNEL_EVENT_BODY_LIMIT_BYTES = 8 * 1024
JOHANNA_FUNNEL_EVENT_MAX_AGE = timedelta(days=31)
JOHANNA_FUNNEL_EVENT_FUTURE_TOLERANCE = timedelta(minutes=5)
CHATWOOT_CONVERSATION_RESET_COMMAND = "/nuevo"
CHATWOOT_CONVERSATION_RESET_CONFIRMATION = "Memoria eliminada."
PRECHECKOUT_FIRST_TOUCH_TEMPLATE_NAME = "libre_ansiedad_test_first_touch_v1"
PRECHECKOUT_FIRST_TOUCH_COPY_VERSION = "libre-ansiedad-precheckout-first-touch-v1"
JOHANNA_ABANDONMENT_TEMPLATE_NAME = "johanna_carrito_abandonado_01"
JOHANNA_ABANDONMENT_COPY_VERSION = "johanna-abandonment-one-shot-v1"
JOHANNA_PAYMENT_FAILURE_TEMPLATE_NAME = "johanna_compra_fallida_01"
JOHANNA_PAYMENT_FAILURE_COPY_VERSION = "johanna-payment-failure-one-shot-v1"
JOHANNA_ABANDONMENT_BODY_LIMIT_BYTES = 8 * 1024
PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES = frozenset({
    "chatwoot_human_pause_enabled",
    # Levantar la pausa no depende del aliado: lo unico especifico es el id del
    # macro de Chatwoot, que ya es configuracion por runtime.
    "conversation_resume_enabled",
    # Reactivar tampoco depende del aliado: lo especifico es el nombre de la
    # plantilla aprobada, que ya es configuracion por runtime.
    "conversation_reactivation_enabled",
    "hermes_shadow_enabled",
    "automated_replies_enabled",
    "reply_splitter_enabled",
    "portable_hotmart_recovery_enabled",
    "portable_hotmart_payment_failure_enabled",
    "portable_hotmart_purchase_stop_enabled",
    "hotmart_purchase_worker_enabled",
    "lead_precheckout_enabled",
    # El adaptador de GHL traduce el formulario a lead.precheckout y lo manda
    # a la misma admision portable que /webhooks/lead; lo especifico (que
    # formularios) sale de [adaptadores.ghl] del manifiesto.
    "ghl_precheckout_adapter_enabled",
    # El primer contacto tras el formulario solo existe con manifiesto: el
    # flujo, el evento y la plantilla salen de la instancia, y el scope del
    # piloto de LANCEMOS_PILOT_PRECHECKOUT_SCOPE_*.
    "portable_precheckout_first_contact_enabled",
    "worker_enabled",
    "dispatcher_enabled",
    "dispatcher_outbound_enabled",
    # El dispatcher manda la plantilla aprobada del catalogo sin pedirle
    # borrador a Hermes; lo especifico (nombre, idioma, variables) sale de
    # [plantillas] del manifiesto.
    "dispatcher_approved_template_direct_enabled",
    "meta_final_effect_enabled",
    "chatwoot_durable_opt_out_enabled",
    "human_handoff_projection_enabled",
    "human_handoff_admission_enabled",
    "pilot_boundary_enabled",
    "chatwoot_cut_b_admission_enabled",
    "chatwoot_cut_b_agent_enabled",
    "payment_link_enabled",
    "chatwoot_post_inbound_discount_planning_enabled",
    "chatwoot_scoped_inbound_senders_enabled",
    "chatwoot_stalled_monitor_enabled",
    "operator_correlation_read_enabled",
    "operator_correlation_write_enabled",
    "slack_connector_projection_enabled",
    "correlation_preresolution_enabled",
    # Transcribir un audio no depende del aliado: la key y el host de Chatwoot
    # ya son configuracion por runtime.
    "chatwoot_audio_transcription_enabled",
    # Saludar por el primer nombre no depende del aliado: la regla
    # deterministica y las inferencias guardadas son del producto. En un
    # runtime portable solo lo usa el dispatcher en modo plantilla directa.
    "lead_first_name_greeting_enabled",
})

DEFAULT_SENSITIVE_SUBJECTS = (
    "medicacion", "medicamento", "farmaco", "pastilla", "antidepresiv", "ansiolitic",
)
DEFAULT_SENSITIVE_ACTIONS = (
    "dejar", "suspender", "interrumpir", "cambiar", "reducir", "aumentar",
    "tomar", "dosis", "dosificacion",
)


def _fold(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )


def _stem_pattern(stems: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(
        r"\b(?:" + "|".join(re.escape(_fold(stem)) for stem in stems) + r")\w*\b"
    )


_MEDICATION_GUIDANCE_SUBJECT_RE = _stem_pattern(DEFAULT_SENSITIVE_SUBJECTS)
_MEDICATION_GUIDANCE_ACTION_RE = _stem_pattern(DEFAULT_SENSITIVE_ACTIONS)


def _requires_medication_guidance_handoff(
    content: object,
    *,
    subject_re: re.Pattern[str] = _MEDICATION_GUIDANCE_SUBJECT_RE,
    action_re: re.Pattern[str] = _MEDICATION_GUIDANCE_ACTION_RE,
) -> bool:
    if not isinstance(content, str):
        return False
    normalized = _fold(content)
    return bool(subject_re.search(normalized) and action_re.search(normalized))


class CanonicalHistoryIncompleteError(RetryableChatwootWorkError):
    """Raised when Chatwoot has not exposed the triggering message yet."""


@dataclass(frozen=True)
class CanonicalWorkResult:
    proposal: dict[str, object] | None
    stopped: bool = False


class ChatwootInboundAdmissionRejectedError(RuntimeError):
    """The inbound admission rejected the conversation for good.

    Not a ``RetryableChatwootWorkError`` on purpose: replaying the same
    message cannot change the answer, so the work ends as failed after the
    bounded attempts instead of being retried without limit.
    """


class ChatwootControl(Protocol):
    async def list_stalled_conversations(
        self,
        *,
        expected_inbox_id: int,
        stale_after_seconds: float = 120,
        max_age_seconds: float = 86_400,
        max_pages: int = 5,
        allow_any_scoped_sender: bool = False,
        now_epoch: float | None = None,
    ) -> list[StalledChatwootConversation]: ...

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
        expected_inbox_id: int | None = None,
        expected_jid: str | None = None,
        pre_send_authorizer: Callable[[], Awaitable[bool]] | None = None,
        agent_decision: str | None = None,
        agent_reason_code: str | None = None,
    ) -> dict[str, object]: ...


_AGENT_MARKER_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _agent_marker(value: object) -> str | None:
    """Decision o reason code de la propuesta, solo si tiene la forma cerrada.

    Se estampa en el mensaje de Chatwoot para la revision diaria; un valor con
    otra forma se omite en vez de romper el envio.
    """
    if isinstance(value, str) and _AGENT_MARKER_RE.fullmatch(value):
        return value
    return None


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
    commercial_ally_config: CommercialAllyConfig = JOHANNA_COMMERCIAL_ALLY
    commercial_ally_manifest_path: Path | None = None
    # Manifiesto de instancia v2 (INSTANCE_MANIFEST_PATH). Cuando esta, el binding
    # sale de el y sus flujos declarados son el techo de los flags del runtime.
    instance_manifest: InstanceManifest | None = None
    # Conocimiento comercial aprobado de la instancia, que viaja como `system`.
    commercial_knowledge: CommercialKnowledge | None = None
    agent_bot_id: int | None = None
    chatwoot_base_url: str | None = None
    chatwoot_account_id: int | None = None
    chatwoot_control_api_access_token: str | None = None
    chatwoot_agent_bot_access_token: str | None = None
    chatwoot_pause_macro_id: int | None = None
    chatwoot_human_pause_enabled: bool = False
    chatwoot_opt_out_macro_id: int | None = None
    chatwoot_resume_macro_id: int | None = None
    conversation_resume_enabled: bool = False
    conversation_resume_quiet_seconds: int = 28800
    conversation_resume_max: int = 3
    conversation_reactivation_enabled: bool = False
    conversation_reactivation_template_name: str | None = None
    conversation_reactivation_template_language: str | None = None
    conversation_reactivation_interval_seconds: float = 900.0
    conversation_reactivation_min_age_seconds: int = 86_400
    conversation_reactivation_max_age_seconds: int = 2_592_000
    conversation_reactivation_max: int = 1
    conversation_reactivation_max_sends_per_scan: int = 10
    conversation_reactivation_max_pages: int = 5
    conversation_followup_enabled: bool = False
    conversation_followup_template_name: str | None = None
    conversation_followup_template_language: str | None = None
    conversation_followup_coupon_code: str | None = None
    conversation_followup_product_name: str | None = None
    conversation_followup_interval_seconds: float = 300.0
    conversation_followup_min_age_seconds: int = 86_400
    conversation_followup_max_age_seconds: int = 259_200
    conversation_followup_max_sends_per_scan: int = 10
    conversation_followup_max_pages: int = 5
    hermes_shadow_enabled: bool = False
    hermes_api_base_url: str | None = None
    hermes_api_key: str | None = None
    hermes_model_name: str = "agente-comercial"
    # De que despliegue del bridge salio cada turno. Viene del entorno del
    # contenedor, que ya lo tiene cargado; sin el, "unknown" es honesto.
    bridge_release: str = "unknown"
    # Con que tenant y scope se anota la procedencia. Los defaults son los
    # reales del inbox de Johanna --- los mismos con los que la revision
    # diaria indexa sus lotes --- para que el registro arranque con el
    # redeploy y no dependa de que alguien cargue una variable.
    agent_provenance_tenant_ref: str = "lancemos"
    agent_provenance_scope_ref: str = "psicologajohanna-agent-bot-19"
    shadow_dir: Path = Path("./data/shadow")
    automated_replies_enabled: bool = False
    reply_dir: Path = Path("./data/replies")
    reply_splitter_enabled: bool = False
    reply_splitter_provider: str | None = None
    reply_splitter_model_name: str | None = None
    reply_part_delay_seconds: float = 2.0
    chatwoot_inbound_debounce_seconds: float = 0.0
    chatwoot_stalled_monitor_enabled: bool = False
    chatwoot_stalled_monitor_interval_seconds: float = 60.0
    chatwoot_stalled_after_seconds: float = 120.0
    chatwoot_stalled_max_age_seconds: float = 86_400.0
    chatwoot_stalled_max_pages: int = 5
    chatwoot_stalled_recovery_cooldown_seconds: float = 300.0
    chatwoot_stalled_max_recovery_admissions: int = 3
    hotmart_hottok: str | None = None
    hotmart_max_age_seconds: int = 300
    portable_hotmart_recovery_enabled: bool = False
    portable_hotmart_payment_failure_enabled: bool = False
    portable_hotmart_purchase_stop_enabled: bool = False
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
    ghl_precheckout_adapter_enabled: bool = False
    ghl_precheckout_adapter_token: str | None = None
    precheckout_first_touch_enabled: bool = False
    precheckout_first_touch_token: str | None = None
    precheckout_delayed_first_touch_enabled: bool = False
    precheckout_delayed_outbound_enabled: bool = False
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
    # The dispatcher sends the approved template of the Chatwoot catalog without
    # a Hermes draft (docs/contracts/approved-template-direct-dispatch-v1.md).
    dispatcher_approved_template_direct_enabled: bool = False
    meta_final_effect_enabled: bool = False
    meta_final_effect_evidence_dir: Path = Path("./data/meta-final-effect-gate")
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
    # Primer contacto portable tras el formulario de la landing (migracion
    # 20261001000200). Apagado, el formulario solo deja la intencion, como
    # hasta ahora. Prendido, la admision tambien planifica el primer contacto
    # contra este scope (fuente landing), que es otro que el de recuperacion, y
    # el dispatcher lo manda con la plantilla de [plantillas.precheckout].
    portable_precheckout_first_contact_enabled: bool = False
    pilot_precheckout_scope_key: str | None = None
    pilot_precheckout_scope_version: int | None = None
    waba_first_touch_template_name: str | None = None
    waba_payment_failure_template_name: str | None = None
    waba_precheckout_template_name: str | None = None
    waba_followup_template_name: str | None = None
    waba_template_language: str | None = None
    waba_template_category: str | None = None
    waba_payment_failure_template_category: str | None = None
    chatwoot_cut_b_admission_enabled: bool = False
    chatwoot_cut_b_scope_key: str | None = None
    chatwoot_cut_b_scope_version: int | None = None
    chatwoot_cut_b_agent_enabled: bool = False
    payment_link_enabled: bool = False
    payment_link_tracking_fields: tuple[str, ...] = ("src", "xcod")
    payment_link_tracking_prefix: str = "hermes-"
    payment_link_max_age_seconds: int = 604800
    # La plantilla de WhatsApp que lleva el link en su boton
    # (docs/contracts/johanna-payment-link-v2.md). Sin nombre, el link sale
    # escrito en el mensaje, como siempre.
    payment_link_template_name: str | None = None
    payment_link_template_language: str | None = None
    chatwoot_post_inbound_discount_planning_enabled: bool = False
    commercial_ally_discount_policy_key: str | None = None
    commercial_ally_discount_policy_version: int | None = None
    chatwoot_scoped_inbound_senders_enabled: bool = False
    operator_correlation_read_enabled: bool = False
    operator_correlation_read_token: str | None = None
    operator_correlation_tenant_ref: str | None = None
    operator_correlation_funnel_ref: str | None = None
    operator_correlation_write_enabled: bool = False
    operator_correlation_write_token: str | None = None
    operator_correlation_actor_ref: str | None = None
    operator_correlation_actor_prefix: str | None = None
    slack_connector_projection_enabled: bool = False
    slack_connector_base_url: str | None = None
    slack_connector_bearer_token: str | None = None
    slack_connector_worker_id: str | None = None
    slack_connector_poll_interval_seconds: float = 5.0
    slack_connector_batch_size: int = 1
    slack_connector_lease_seconds: int = 60
    correlation_preresolution_enabled: bool = False
    correlation_preresolution_model_name: str | None = None
    correlation_preresolution_prompt_version: str = "correlation-preresolution-v3"
    correlation_preresolution_worker_id: str | None = None
    correlation_preresolution_poll_interval_seconds: float = 5.0
    # La cadena de tres niveles del saludo (ver bridge.lead_first_name). Sola,
    # ya saluda por el primer nombre deterministico; con la inferencia prendida,
    # usa ademas lo que el modelo guardo al llegar el formulario.
    lead_first_name_greeting_enabled: bool = False
    lead_first_name_inference_enabled: bool = False
    lead_first_name_model_name: str | None = None
    # Los audios entrantes se transcriben por OpenRouter antes de pasarle el
    # mensaje al agente (ver bridge.audio_transcription). Apagado, un audio
    # sigue sin respuesta, pero ahora deja una linea en el log.
    chatwoot_audio_transcription_enabled: bool = False
    openrouter_api_key: str | None = None
    audio_transcription_models: tuple[str, ...] = DEFAULT_TRANSCRIPTION_MODELS
    audio_transcription_cache_dir: Path = Path("./data/audio-transcriptions")

    @classmethod
    def from_env(cls) -> Settings:
        commercial_ally_config_path = os.getenv(
            "COMMERCIAL_ALLY_CONFIG_PATH", ""
        ).strip()
        instance_manifest_path = os.getenv("INSTANCE_MANIFEST_PATH", "").strip()
        if instance_manifest_path and commercial_ally_config_path:
            raise ValueError(
                "INSTANCE_MANIFEST_PATH and COMMERCIAL_ALLY_CONFIG_PATH are "
                "mutually exclusive"
            )
        instance_manifest = (
            InstanceManifest.from_toml_file(Path(instance_manifest_path))
            if instance_manifest_path
            else None
        )
        if instance_manifest is not None:
            commercial_ally_config = instance_manifest.to_commercial_ally_config()
        else:
            commercial_ally_config = (
                CommercialAllyConfig.from_json_file(Path(commercial_ally_config_path))
                if commercial_ally_config_path
                else JOHANNA_COMMERCIAL_ALLY
            )
        commercial_knowledge: CommercialKnowledge | None = None
        if os.getenv("COMMERCIAL_KNOWLEDGE_ENABLED", "false").lower() == "true":
            if instance_manifest is None:
                raise ValueError(
                    "COMMERCIAL_KNOWLEDGE_ENABLED requires INSTANCE_MANIFEST_PATH"
                )
            commercial_knowledge = CommercialKnowledge.from_toml_file(
                Path(instance_manifest_path).parent
                / instance_manifest.agent_knowledge_path
            )
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
        chatwoot_stalled_monitor_enabled = (
            os.getenv("CHATWOOT_STALLED_MONITOR_ENABLED", "false").lower()
            == "true"
        )
        chatwoot_stalled_monitor_interval_seconds = float(
            os.getenv("CHATWOOT_STALLED_MONITOR_INTERVAL_SECONDS", "60")
        )
        chatwoot_stalled_after_seconds = float(
            os.getenv("CHATWOOT_STALLED_AFTER_SECONDS", "120")
        )
        chatwoot_stalled_max_age_seconds = float(
            os.getenv("CHATWOOT_STALLED_MAX_AGE_SECONDS", "86400")
        )
        chatwoot_stalled_max_pages = int(
            os.getenv("CHATWOOT_STALLED_MAX_PAGES", "5")
        )
        chatwoot_stalled_recovery_cooldown_seconds = float(
            os.getenv("CHATWOOT_STALLED_RECOVERY_COOLDOWN_SECONDS", "300")
        )
        chatwoot_stalled_max_recovery_admissions = int(
            os.getenv("CHATWOOT_STALLED_MAX_RECOVERY_ADMISSIONS", "3")
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
        bridge_release = (
            os.getenv("GIT_SHA") or os.getenv("BRIDGE_RELEASE") or "unknown"
        ).strip()[:128] or "unknown"
        agent_provenance_tenant_ref = (
            os.getenv("AGENT_PROVENANCE_TENANT_REF") or "lancemos"
        )
        agent_provenance_scope_ref = (
            os.getenv("AGENT_PROVENANCE_SCOPE_REF")
            or "psicologajohanna-agent-bot-19"
        )
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
        portable_hotmart_purchase_stop_enabled = (
            os.getenv("PORTABLE_HOTMART_PURCHASE_STOP_ENABLED", "false").lower()
            == "true"
        )
        portable_hotmart_recovery_enabled = (
            os.getenv("PORTABLE_HOTMART_RECOVERY_ENABLED", "false").lower()
            == "true"
        )
        portable_hotmart_payment_failure_enabled = (
            os.getenv(
                "PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED", "false"
            ).lower()
            == "true"
        )
        portable_precheckout_first_contact_enabled = (
            os.getenv(
                "PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED", "false"
            ).lower()
            == "true"
        )
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
        precheckout_delayed_outbound_value = os.getenv(
            "PRECHECKOUT_DELAYED_OUTBOUND_ENABLED", "false"
        ).strip().lower()
        if precheckout_delayed_outbound_value not in {"true", "false"}:
            raise ValueError(
                "PRECHECKOUT_DELAYED_OUTBOUND_ENABLED must be true or false"
            )
        precheckout_delayed_outbound_enabled = (
            precheckout_delayed_outbound_value == "true"
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
            "LEAD_PRECHECKOUT_SITE", commercial_ally_config.lead_site
        ).strip()
        lead_precheckout_landing_id = os.getenv(
            "LEAD_PRECHECKOUT_LANDING_ID", commercial_ally_config.lead_landing_id
        ).strip()
        lead_precheckout_offer_code = os.getenv(
            "LEAD_PRECHECKOUT_OFFER_CODE", commercial_ally_config.offer_code
        ).strip()
        ghl_precheckout_adapter_enabled = (
            os.getenv("GHL_PRECHECKOUT_ADAPTER_ENABLED", "false").lower() == "true"
        )
        ghl_precheckout_adapter_token = (
            os.getenv("GHL_PRECHECKOUT_ADAPTER_TOKEN", "").strip() or None
        )
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
        if ghl_precheckout_adapter_enabled and (
            ghl_precheckout_adapter_token is None
            or len(ghl_precheckout_adapter_token) < 32
        ):
            raise ValueError(
                "GHL_PRECHECKOUT_ADAPTER_TOKEN must contain at least 32 characters"
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
        chatwoot_account_id = int(os.environ["CHATWOOT_ACCOUNT_ID"])
        chatwoot_inbox_id_raw = os.getenv("CHATWOOT_INBOX_ID", "").strip()
        chatwoot_inbox_id = int(chatwoot_inbox_id_raw) if chatwoot_inbox_id_raw else None
        configured_chatwoot_scope = (chatwoot_account_id, chatwoot_inbox_id)
        expected_chatwoot_scope = (
            commercial_ally_config.chatwoot_account_id,
            commercial_ally_config.chatwoot_inbox_id,
        )
        if commercial_ally_config_path or instance_manifest_path:
            if configured_chatwoot_scope != expected_chatwoot_scope:
                raise ValueError(
                    "Chatwoot account and inbox must match commercial ally config"
                )
        elif chatwoot_account_id != 1 or chatwoot_inbox_id not in {None, 9}:
            raise ValueError(
                "COMMERCIAL_ALLY_CONFIG_PATH is required for non-legacy Chatwoot scope"
            )
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
        dispatcher_approved_template_direct_enabled = (
            os.getenv("DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED", "false").lower()
            == "true"
        )
        meta_final_effect_value = os.getenv(
            "META_FINAL_EFFECT_ENABLED", "false"
        ).strip().lower()
        if meta_final_effect_value not in {"true", "false"}:
            raise ValueError("META_FINAL_EFFECT_ENABLED must be true or false")
        meta_final_effect_enabled = meta_final_effect_value == "true"
        meta_final_effect_evidence_dir = Path(
            os.getenv(
                "META_FINAL_EFFECT_EVIDENCE_DIR",
                "./data/meta-final-effect-gate",
            )
        )
        chatwoot_durable_opt_out_enabled = (
            os.getenv("CHATWOOT_DURABLE_OPT_OUT_ENABLED", "false").lower()
            == "true"
        )
        opt_out_macro_id_raw = os.getenv("CHATWOOT_OPT_OUT_MACRO_ID", "").strip()
        chatwoot_opt_out_macro_id = (
            int(opt_out_macro_id_raw) if opt_out_macro_id_raw else None
        )
        resume_macro_id_raw = os.environ.get(
            "CHATWOOT_RESUME_MACRO_ID", ""
        ).strip()
        chatwoot_resume_macro_id = (
            int(resume_macro_id_raw) if resume_macro_id_raw else None
        )
        conversation_resume_enabled = (
            os.environ.get("CONVERSATION_RESUME_ENABLED", "false").lower()
            == "true"
        )
        conversation_resume_quiet_seconds = int(
            os.environ.get("CONVERSATION_RESUME_QUIET_SECONDS", "28800")
        )
        conversation_resume_max = int(
            os.environ.get("CONVERSATION_RESUME_MAX", "3")
        )
        conversation_reactivation_enabled = (
            os.environ.get("CONVERSATION_REACTIVATION_ENABLED", "false").lower()
            == "true"
        )
        conversation_reactivation_template_name = (
            os.environ.get("WABA_REACTIVATION_TEMPLATE_NAME", "").strip() or None
        )
        conversation_reactivation_template_language = (
            os.environ.get("WABA_REACTIVATION_TEMPLATE_LANGUAGE", "").strip()
            or None
        )
        conversation_reactivation_interval_seconds = float(
            os.environ.get("CONVERSATION_REACTIVATION_INTERVAL_SECONDS", "900")
        )
        conversation_reactivation_min_age_seconds = int(
            os.environ.get("CONVERSATION_REACTIVATION_MIN_AGE_SECONDS", "86400")
        )
        conversation_reactivation_max_age_seconds = int(
            os.environ.get(
                "CONVERSATION_REACTIVATION_MAX_AGE_SECONDS", "2592000"
            )
        )
        conversation_reactivation_max = int(
            os.environ.get("CONVERSATION_REACTIVATION_MAX", "1")
        )
        conversation_reactivation_max_sends_per_scan = int(
            os.environ.get(
                "CONVERSATION_REACTIVATION_MAX_SENDS_PER_SCAN", "10"
            )
        )
        conversation_reactivation_max_pages = int(
            os.environ.get("CONVERSATION_REACTIVATION_MAX_PAGES", "5")
        )
        conversation_followup_enabled = (
            os.environ.get("CONVERSATION_FOLLOWUP_ENABLED", "false").lower()
            == "true"
        )
        conversation_followup_template_name = (
            os.environ.get("CONVERSATION_FOLLOWUP_TEMPLATE_NAME", "").strip()
            or None
        )
        conversation_followup_template_language = (
            os.environ.get("CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE", "").strip()
            or None
        )
        conversation_followup_coupon_code = (
            os.environ.get("CONVERSATION_FOLLOWUP_COUPON_CODE", "").strip() or None
        )
        conversation_followup_product_name = (
            os.environ.get("CONVERSATION_FOLLOWUP_PRODUCT_NAME", "").strip()
            or None
        )
        conversation_followup_interval_seconds = float(
            os.environ.get("CONVERSATION_FOLLOWUP_INTERVAL_SECONDS", "300")
        )
        conversation_followup_min_age_seconds = int(
            os.environ.get("CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS", "86400")
        )
        conversation_followup_max_age_seconds = int(
            os.environ.get("CONVERSATION_FOLLOWUP_MAX_AGE_SECONDS", "259200")
        )
        conversation_followup_max_sends_per_scan = int(
            os.environ.get("CONVERSATION_FOLLOWUP_MAX_SENDS_PER_SCAN", "10")
        )
        conversation_followup_max_pages = int(
            os.environ.get("CONVERSATION_FOLLOWUP_MAX_PAGES", "5")
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
        pilot_precheckout_scope_version_raw = os.getenv(
            "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION", ""
        ).strip()
        chatwoot_cut_b_scope_version_raw = os.getenv(
            "CHATWOOT_CUT_B_SCOPE_VERSION", ""
        ).strip()
        commercial_ally_discount_policy_version_raw = os.getenv(
            "COMMERCIAL_ALLY_DISCOUNT_POLICY_VERSION", ""
        ).strip()
        slack_connector_projection_enabled = (
            os.getenv("SLACK_CONNECTOR_PROJECTION_ENABLED", "false").lower()
            == "true"
        )
        correlation_preresolution_enabled = (
            os.getenv("CORRELATION_PRERESOLUTION_ENABLED", "false").lower()
            == "true"
        )
        correlation_preresolution_model_name = (
            os.getenv("CORRELATION_PRERESOLUTION_MODEL_NAME", "").strip()
            or hermes_model_name
        )
        correlation_preresolution_prompt_version = os.getenv(
            "CORRELATION_PRERESOLUTION_PROMPT_VERSION",
            "correlation-preresolution-v3",
        ).strip()
        correlation_preresolution_worker_id = (
            os.getenv("CORRELATION_PRERESOLUTION_WORKER_ID", "").strip() or None
        )
        correlation_preresolution_poll_interval_seconds = float(
            os.getenv("CORRELATION_PRERESOLUTION_POLL_INTERVAL", "5.0")
        )
        if correlation_preresolution_enabled and (
            hermes_api_base_url is None
            or hermes_api_key is None
            or not correlation_preresolution_model_name
            or not correlation_preresolution_prompt_version
            or correlation_preresolution_worker_id is None
            or supabase_base_url is None
            or supabase_service_role_key is None
        ):
            raise ValueError("correlation_preresolution_configuration_incomplete")
        lead_first_name_greeting_enabled = (
            os.getenv("LEAD_FIRST_NAME_GREETING_ENABLED", "false").lower()
            == "true"
        )
        lead_first_name_inference_enabled = (
            os.getenv("LEAD_FIRST_NAME_INFERENCE_ENABLED", "false").lower()
            == "true"
        )
        lead_first_name_model_name = (
            os.getenv("LEAD_FIRST_NAME_MODEL_NAME", "").strip()
            or hermes_model_name
        )
        if lead_first_name_inference_enabled and (
            not lead_first_name_greeting_enabled
            or hermes_api_base_url is None
            or hermes_api_key is None
            or not lead_first_name_model_name
            or supabase_base_url is None
            or supabase_service_role_key is None
        ):
            raise ValueError("lead_first_name_configuration_incomplete")
        chatwoot_audio_transcription_enabled = (
            os.getenv("CHATWOOT_AUDIO_TRANSCRIPTION_ENABLED", "false").lower()
            == "true"
        )
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip() or None
        audio_transcription_models = tuple(
            model.strip()
            for model in os.getenv(
                "AUDIO_TRANSCRIPTION_MODELS",
                ",".join(DEFAULT_TRANSCRIPTION_MODELS),
            ).split(",")
            if model.strip()
        )
        if chatwoot_audio_transcription_enabled and (
            openrouter_api_key is None or not audio_transcription_models
        ):
            raise ValueError("audio_transcription_configuration_incomplete")
        if (
            not math.isfinite(correlation_preresolution_poll_interval_seconds)
            or correlation_preresolution_poll_interval_seconds <= 0
        ):
            raise ValueError("invalid_correlation_preresolution_poll_interval")
        if slack_connector_projection_enabled and not correlation_preresolution_enabled:
            raise ValueError(
                "SLACK_CONNECTOR_PROJECTION_ENABLED requires "
                "CORRELATION_PRERESOLUTION_ENABLED"
            )

        return cls(
            webhook_secret=os.environ["CHATWOOT_WEBHOOK_SECRET"],
            allowed_jid=allowed_jid,
            capture_dir=Path(os.getenv("CAPTURE_DIR", "./data/captures")),
            max_age_seconds=int(os.getenv("WEBHOOK_MAX_AGE_SECONDS", "300")),
            commercial_ally_config=commercial_ally_config,
            commercial_ally_manifest_path=(
                Path(instance_manifest_path or commercial_ally_config_path)
                if instance_manifest_path or commercial_ally_config_path
                else None
            ),
            instance_manifest=instance_manifest,
            commercial_knowledge=commercial_knowledge,
            agent_bot_id=int(os.environ["CHATWOOT_AGENT_BOT_ID"]),
            chatwoot_base_url=os.environ["CHATWOOT_BASE_URL"],
            chatwoot_account_id=chatwoot_account_id,
            chatwoot_control_api_access_token=os.environ[
                "CHATWOOT_CONTROL_API_ACCESS_TOKEN"
            ],
            chatwoot_agent_bot_access_token=agent_bot_access_token,
            chatwoot_pause_macro_id=int(os.environ["CHATWOOT_PAUSE_MACRO_ID"]),
            chatwoot_human_pause_enabled=chatwoot_human_pause_enabled,
            chatwoot_opt_out_macro_id=chatwoot_opt_out_macro_id,
            chatwoot_resume_macro_id=chatwoot_resume_macro_id,
            conversation_resume_enabled=conversation_resume_enabled,
            conversation_resume_quiet_seconds=conversation_resume_quiet_seconds,
            conversation_resume_max=conversation_resume_max,
            conversation_reactivation_enabled=conversation_reactivation_enabled,
            conversation_reactivation_template_name=(
                conversation_reactivation_template_name
            ),
            conversation_reactivation_template_language=(
                conversation_reactivation_template_language
            ),
            conversation_reactivation_interval_seconds=(
                conversation_reactivation_interval_seconds
            ),
            conversation_reactivation_min_age_seconds=(
                conversation_reactivation_min_age_seconds
            ),
            conversation_reactivation_max_age_seconds=(
                conversation_reactivation_max_age_seconds
            ),
            conversation_reactivation_max=conversation_reactivation_max,
            conversation_reactivation_max_sends_per_scan=(
                conversation_reactivation_max_sends_per_scan
            ),
            conversation_reactivation_max_pages=(
                conversation_reactivation_max_pages
            ),
            conversation_followup_enabled=conversation_followup_enabled,
            conversation_followup_template_name=(
                conversation_followup_template_name
            ),
            conversation_followup_template_language=(
                conversation_followup_template_language
            ),
            conversation_followup_coupon_code=conversation_followup_coupon_code,
            conversation_followup_product_name=(
                conversation_followup_product_name
            ),
            conversation_followup_interval_seconds=(
                conversation_followup_interval_seconds
            ),
            conversation_followup_min_age_seconds=(
                conversation_followup_min_age_seconds
            ),
            conversation_followup_max_age_seconds=(
                conversation_followup_max_age_seconds
            ),
            conversation_followup_max_sends_per_scan=(
                conversation_followup_max_sends_per_scan
            ),
            conversation_followup_max_pages=conversation_followup_max_pages,
            hermes_shadow_enabled=shadow_enabled,
            hermes_api_base_url=hermes_api_base_url,
            hermes_api_key=hermes_api_key,
            hermes_model_name=hermes_model_name,
            bridge_release=bridge_release,
            agent_provenance_tenant_ref=agent_provenance_tenant_ref,
            agent_provenance_scope_ref=agent_provenance_scope_ref,
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
            chatwoot_stalled_monitor_enabled=chatwoot_stalled_monitor_enabled,
            chatwoot_stalled_monitor_interval_seconds=(
                chatwoot_stalled_monitor_interval_seconds
            ),
            chatwoot_stalled_after_seconds=chatwoot_stalled_after_seconds,
            chatwoot_stalled_max_age_seconds=chatwoot_stalled_max_age_seconds,
            chatwoot_stalled_max_pages=chatwoot_stalled_max_pages,
            chatwoot_stalled_recovery_cooldown_seconds=(
                chatwoot_stalled_recovery_cooldown_seconds
            ),
            chatwoot_stalled_max_recovery_admissions=(
                chatwoot_stalled_max_recovery_admissions
            ),
            hotmart_hottok=hotmart_hottok,
            hotmart_max_age_seconds=hotmart_max_age_seconds,
            portable_hotmart_purchase_stop_enabled=(
                portable_hotmart_purchase_stop_enabled
            ),
            portable_hotmart_recovery_enabled=portable_hotmart_recovery_enabled,
            portable_hotmart_payment_failure_enabled=(
                portable_hotmart_payment_failure_enabled
            ),
            portable_precheckout_first_contact_enabled=(
                portable_precheckout_first_contact_enabled
            ),
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
            ghl_precheckout_adapter_enabled=ghl_precheckout_adapter_enabled,
            ghl_precheckout_adapter_token=ghl_precheckout_adapter_token,
            precheckout_first_touch_enabled=precheckout_first_touch_enabled,
            precheckout_first_touch_token=precheckout_first_touch_token,
            precheckout_delayed_first_touch_enabled=(
                precheckout_delayed_first_touch_enabled
            ),
            precheckout_delayed_outbound_enabled=(
                precheckout_delayed_outbound_enabled
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
            dispatcher_approved_template_direct_enabled=(
                dispatcher_approved_template_direct_enabled
            ),
            meta_final_effect_enabled=meta_final_effect_enabled,
            meta_final_effect_evidence_dir=meta_final_effect_evidence_dir,
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
            pilot_precheckout_scope_key=(
                os.getenv("LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY", "").strip()
                or None
            ),
            pilot_precheckout_scope_version=(
                int(pilot_precheckout_scope_version_raw)
                if pilot_precheckout_scope_version_raw
                else None
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
            waba_payment_failure_template_name=(
                os.getenv("WABA_PAYMENT_FAILURE_TEMPLATE_NAME", "").strip() or None
            ),
            waba_precheckout_template_name=(
                os.getenv("WABA_PRECHECKOUT_TEMPLATE_NAME", "").strip() or None
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
            waba_payment_failure_template_category=(
                os.getenv("WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY", "")
                .strip()
                .upper()
                or None
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
            payment_link_enabled=(
                os.getenv("PAYMENT_LINK_ENABLED", "false").lower() == "true"
            ),
            payment_link_tracking_fields=tuple(
                field.strip()
                for field in os.getenv(
                    "PAYMENT_LINK_TRACKING_FIELDS", "src,xcod"
                ).split(",")
                if field.strip()
            ),
            payment_link_tracking_prefix=os.getenv(
                "PAYMENT_LINK_TRACKING_PREFIX", "hermes-"
            ).strip(),
            payment_link_max_age_seconds=int(
                os.getenv("PAYMENT_LINK_MAX_AGE_SECONDS", "604800")
            ),
            payment_link_template_name=(
                os.getenv("PAYMENT_LINK_TEMPLATE_NAME", "").strip() or None
            ),
            payment_link_template_language=(
                os.getenv("PAYMENT_LINK_TEMPLATE_LANGUAGE", "").strip() or None
            ),
            chatwoot_post_inbound_discount_planning_enabled=(
                os.getenv(
                    "CHATWOOT_POST_INBOUND_DISCOUNT_PLANNING_ENABLED", "false"
                ).lower()
                == "true"
            ),
            commercial_ally_discount_policy_key=(
                os.getenv("COMMERCIAL_ALLY_DISCOUNT_POLICY_KEY", "").strip()
                or None
            ),
            commercial_ally_discount_policy_version=(
                int(commercial_ally_discount_policy_version_raw)
                if commercial_ally_discount_policy_version_raw
                else None
            ),
            slack_connector_projection_enabled=slack_connector_projection_enabled,
            slack_connector_base_url=(
                os.getenv("SLACK_CONNECTOR_BASE_URL", "").strip() or None
            ),
            slack_connector_bearer_token=(
                os.getenv("SLACK_CONNECTOR_BEARER_TOKEN", "").strip() or None
            ),
            slack_connector_worker_id=(
                os.getenv("SLACK_CONNECTOR_WORKER_ID", "").strip() or None
            ),
            slack_connector_poll_interval_seconds=float(
                os.getenv("SLACK_CONNECTOR_POLL_INTERVAL_SECONDS", "5")
            ),
            slack_connector_batch_size=int(
                os.getenv("SLACK_CONNECTOR_BATCH_SIZE", "1")
            ),
            slack_connector_lease_seconds=int(
                os.getenv("SLACK_CONNECTOR_LEASE_SECONDS", "60")
            ),
            correlation_preresolution_enabled=correlation_preresolution_enabled,
            correlation_preresolution_model_name=(
                correlation_preresolution_model_name
            ),
            correlation_preresolution_prompt_version=(
                correlation_preresolution_prompt_version
            ),
            correlation_preresolution_worker_id=correlation_preresolution_worker_id,
            correlation_preresolution_poll_interval_seconds=(
                correlation_preresolution_poll_interval_seconds
            ),
            lead_first_name_greeting_enabled=lead_first_name_greeting_enabled,
            lead_first_name_inference_enabled=lead_first_name_inference_enabled,
            lead_first_name_model_name=lead_first_name_model_name,
            chatwoot_audio_transcription_enabled=(
                chatwoot_audio_transcription_enabled
            ),
            openrouter_api_key=openrouter_api_key,
            audio_transcription_models=audio_transcription_models,
            audio_transcription_cache_dir=Path(
                os.getenv(
                    "AUDIO_TRANSCRIPTION_CACHE_DIR", "./data/audio-transcriptions"
                )
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
            operator_correlation_actor_prefix=(
                os.getenv("OPERATOR_CORRELATION_ACTOR_PREFIX", "").strip() or None
            ),
        )


def _is_conversation_label_change(
    payload: dict[str, object], *, inbox_id: int | None
) -> bool:
    """True para un ``conversation_updated`` del inbox configurado que cambia etiquetas.

    Chatwoot (v4.13.0, ``WebhookListener#conversation_updated``) manda el
    ``webhook_data`` de la conversacion (``id``, ``inbox_id``, ``labels``,
    ``meta``...) mas ``changed_attributes``: una lista de
    ``{atributo: {previous_value, current_value}}``, y ``label_list`` es uno de
    los atributos que disparan el evento. El bridge todavia no lo procesa: lo
    captura para construir la sincronizacion de la etiqueta que toca una
    persona sobre un payload real, no sobre uno supuesto. Hoy la cuenta solo
    suscribe ``message_created``; al suscribir ``conversation_updated`` el
    primer cambio de etiqueta queda en ``CAPTURE_DIR``.
    """
    if inbox_id is None or payload.get("event") != "conversation_updated":
        return False
    observed_inbox = payload.get("inbox_id")
    if (
        not isinstance(observed_inbox, int)
        or isinstance(observed_inbox, bool)
        or observed_inbox != inbox_id
    ):
        return False
    changes = payload.get("changed_attributes")
    if not isinstance(changes, list):
        return False
    return any(
        isinstance(change, dict) and "label_list" in change for change in changes
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
    messages: list[dict[str, object]],
    *,
    agent_bot_id: int,
    include_team_messages: bool = False,
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
        elif (
            include_team_messages
            and message.get("message_type") in (1, 3)
            and sender.get("type") == "user"
        ):
            # Lo que escribio una persona del equipo. Va con actor propio: si
            # entrara como "assistant", el agente sostendria como propias las
            # promesas de un humano.
            actor = "human_agent"
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


async def _resume_paused_conversation(
    *,
    control_client: ChatwootClient,
    supabase: object,
    settings: "Settings",
    conversation_id: int,
    message_id: int,
    expected_jid: str | None = None,
    durable_pause_recorded: bool = False,
) -> bool:
    """Levanta la pausa de una conversacion que el equipo dejo de atender.

    Devuelve True solo cuando la conversacion quedo efectivamente admisible en
    las dos capas: la durable de Supabase (``human_takeover``) y la etiqueta de
    Chatwoot, que es la que gobierna el envio. Cualquier duda devuelve False y
    la conversacion se queda con las personas.

    ``expected_jid`` es el JID del remitente que trajo el webhook (el
    ``contact_inbox.source_id`` canonizado), y es la identidad contra la que el
    cliente verifica la conversacion. Sin el, el cliente compara contra
    ``ALLOWED_WHATSAPP_JID``, que en produccion es el numero de prueba: como el
    show de la API no trae ``contact_inbox`` ni ``meta.sender.identifier``, la
    verificacion cae al telefono y falla para todo lead real con
    ``conversation_identity_mismatch``. Asi estuvo desde el 23/09 hasta el
    25/09/2026: cero filas en ``conversation_resume_events`` mientras la
    conversacion 177 respondia a la plantilla de reactivacion y nadie la
    atendia (fixture
    ``chatwoot_paused_lead_reply_inbox_9_conv_177_20260925.json``).

    ``durable_pause_recorded`` dice si la capa durable sigue pausada (la
    admision devolvio ``blocked``). La etiqueta la saca el sistema al reanudar
    y nunca sola: si no esta pero la pausa durable sigue, una persona la saco
    a mano desde Chatwoot para que el agente vuelva a contestar, y esa
    decision se respeta sin medir el silencio del equipo y sin ejecutar el
    macro. Sin esto, la conversacion quedaba pausada en Supabase para siempre:
    el bridge solo recibe ``message_created``, asi que el cambio de etiqueta
    no le llega (2026-09-25 18:18 UTC, conversacion 173, fixture
    ``chatwoot_paused_lead_label_removed_by_human_inbox_9_conv_173_20260925.json``).
    """
    if settings.chatwoot_inbox_id is None:
        return False
    try:
        snapshot = await control_client.get_canonical_conversation_snapshot(
            conversation_id=conversation_id,
            expected_inbox_id=settings.chatwoot_inbox_id,
            anchor_message_id=None,
            expected_jid=expected_jid,
        )
    except (ChatwootProtocolError, httpx.HTTPError):
        return False
    if snapshot.status != "open" or not snapshot.can_reply:
        return False
    if snapshot.human_assignee_present:
        return False
    if "automation_opted_out" in snapshot.labels:
        return False
    label_removed_by_person = "automation_paused" not in snapshot.labels
    if label_removed_by_person and not durable_pause_recorded:
        # Sin etiqueta y sin pausa durable no hay nada que levantar.
        return False
    quiet_seconds: int | None = None
    if label_removed_by_person:
        # La etiqueta la saca el sistema al reanudar, nunca sola. Si no esta y
        # la capa durable sigue pausada, una persona la saco a mano desde
        # Chatwoot para que el agente vuelva a contestar: se respeta sin
        # medir el silencio del equipo. `operator_request` es el motivo que
        # la RPC admite para una decision humana.
        reason_code = "operator_request"
    else:
        try:
            messages = await control_client.get_conversation_messages(
                conversation_id=conversation_id,
                limit=100,
            )
            quiet_seconds = seconds_since_last_team_message(
                messages, now_epoch=int(time.time())
            )
        except (
            TeamMessageTimestampError,
            ChatwootProtocolError,
            httpx.HTTPError,
        ):
            return False
        if (
            quiet_seconds is not None
            and quiet_seconds < settings.conversation_resume_quiet_seconds
        ):
            logger.info(
                "conversation_resume_skipped reason=team_recently_active quiet=%s",
                quiet_seconds,
            )
            return False
        reason_code = "inbound_after_quiet_period"
    try:
        result = await supabase.resume_paused_conversation(
            external_conversation_id=conversation_id,
            command_key=f"resume:{conversation_id}:{message_id}",
            reason_code=reason_code,
            quiet_seconds=quiet_seconds,
            max_resumes=settings.conversation_resume_max,
        )
    except SupabaseError:
        return False
    if not result.resumed:
        logger.info("conversation_resume_skipped outcome=%s", result.outcome)
        return False
    if not label_removed_by_person:
        try:
            await control_client.clear_conversation_label(
                conversation_id=conversation_id,
                label="automation_paused",
                expected_inbox_id=(
                    settings.chatwoot_inbox_id
                    if expected_jid is not None
                    else None
                ),
                expected_jid=expected_jid,
            )
        except (ChatwootProtocolError, httpx.HTTPError):
            # La capa durable ya quedo admisible, pero sin sacar la etiqueta
            # el envio se bloquea igual: no se reintenta la admision.
            logger.warning(
                "conversation_resume_label_not_cleared conversation=%s",
                conversation_id,
            )
            return False
    logger.info(
        "conversation_resumed conversation=%s outcome=%s reason=%s quiet_seconds=%s",
        conversation_id,
        result.outcome,
        reason_code,
        quiet_seconds,
    )
    return True


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


def _sent_at(message: dict[str, str]) -> dict[str, str]:
    """Fecha de envio del mensaje, para que el agente sepa cuando paso cada cosa.

    Sin esto el historial le llega como un bloque sin tiempo y un pedido de
    hace tres dias pesa igual que el "Hola" de recien (conversacion 173,
    2026-09-24 al 26: tres derivaciones seguidas por un mensaje viejo). El
    `created_at` ya viene de Chatwoot en `_created_at`; solo se deja pasar.
    """
    raw = message.get("_created_at")
    if raw is None:
        return {}
    stamp = datetime.fromtimestamp(int(raw), tz=UTC)
    return {"sent_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}


# Que flujo declarado del manifiesto habilita cada flag del runtime. Un flag
# prendido con su flujo en false no arranca: el manifiesto de la instancia es el
# techo de lo que el runtime puede hacer (docs/referencia-manifiesto.md).
_FLAG_REQUIRED_FLOW = MappingProxyType({
    "automated_replies_enabled": "inbound",
    "chatwoot_cut_b_agent_enabled": "inbound",
    "payment_link_enabled": "inbound",
    "portable_hotmart_recovery_enabled": "carrito",
    "portable_hotmart_payment_failure_enabled": "pago_fallido",
    "portable_precheckout_first_contact_enabled": "precheckout",
    "conversation_reactivation_enabled": "reactivacion",
    "chatwoot_post_inbound_discount_planning_enabled": "descuento",
})
_OUTBOUND_FLOWS = ("precheckout", "carrito", "pago_fallido", "reactivacion", "descuento")


def _validate_instance_manifest_gates(settings: Settings) -> None:
    manifest = settings.instance_manifest
    assert manifest is not None
    if settings.hermes_model_name != manifest.agent_model_name:
        raise ValueError(
            "HERMES_MODEL_NAME must match the instance manifest agent model"
        )
    knowledge = settings.commercial_knowledge
    if knowledge is not None and knowledge.ally_ref != manifest.ally_ref:
        raise ValueError("commercial knowledge belongs to another ally")
    blocked = sorted(
        f"{flag}->{flow}"
        for flag, flow in _FLAG_REQUIRED_FLOW.items()
        if getattr(settings, flag) is True and not manifest.flows[flow]
    )
    if settings.lead_precheckout_enabled and "intencion" not in manifest.events:
        blocked.append("lead_precheckout_enabled->intencion")
    if settings.ghl_precheckout_adapter_enabled:
        # Lo que el adaptador produce es el evento intencion, y solo traduce
        # los formularios que la instancia lista (docs/contracts/
        # ghl-precheckout-adapter-v1.md).
        if "intencion" not in manifest.events:
            blocked.append("ghl_precheckout_adapter_enabled->intencion")
        if not manifest.ghl_form_ids:
            blocked.append("ghl_precheckout_adapter_enabled->adaptadores.ghl")
    if settings.meta_final_effect_enabled and not any(
        manifest.flows[flow] for flow in _OUTBOUND_FLOWS
    ):
        blocked.append("meta_final_effect_enabled->outbound")
    if blocked:
        raise ValueError(
            "runtime flags exceed the instance manifest flows: " + ", ".join(blocked)
        )
    if manifest.ghl_form_ids and manifest.ghl_risk_acceptance is None:
        # El token del adaptador es la unica barrera y lo lee cualquier usuario de
        # la subcuenta de GHL, y una intencion que entro por el adaptador no se
        # distingue en la base de la de una landing. Los dos flujos que usan la
        # intencion como permiso de contacto (el primer contacto del formulario y
        # el pago fallido, que concede el permiso en cualquier audience_mode) no
        # arrancan hasta que alguien acepte ese riesgo por escrito en el
        # manifiesto, o exista una verificacion fuera de banda de cada envio
        # (docs/contracts/ghl-precheckout-adapter-v1.md, Riesgos). Cuelga de la
        # seccion y no del flag: apagar GHL_PRECHECKOUT_ADAPTER_ENABLED no saca
        # de la base las intenciones que el adaptador ya admitio.
        gated = [flow for flow in GHL_RISK_GATED_FLOWS if manifest.flows[flow]]
        if gated:
            raise ValueError(
                "[adaptadores.ghl] cannot run with flujos."
                + ", flujos.".join(gated)
                + " on without the written risk acceptance: the adapter token is "
                "the only barrier and an adapter intent is not told apart from a "
                "landing one. It needs an out-of-band check of each submission or "
                "riesgo_aceptado_por, riesgo_aceptado_el and riesgo_contrato in "
                "[adaptadores.ghl]"
            )
    if settings.automated_replies_enabled and knowledge is None:
        raise ValueError(
            "automated replies with an instance manifest require "
            "COMMERCIAL_KNOWLEDGE_ENABLED"
        )


def _manifest_template_parameters(
    settings: Settings,
) -> tuple[
    tuple[str, ...] | None, tuple[str, ...] | None, tuple[str, ...] | None
]:
    """The body variables of the carrito, pago_fallido and precheckout templates.

    Only for a flow the manifest declares on: its ``[plantillas]`` entry is the
    template that flow sends, so ``WABA_*_TEMPLATE_NAME`` and
    ``WABA_TEMPLATE_LANGUAGE`` must name the same template or the bridge does
    not start. All share the one ``WABA_TEMPLATE_LANGUAGE``, so two flows
    with templates in different languages cannot start either. A flow that is
    off keeps ``None``, the behavior of today.
    """
    manifest = settings.instance_manifest
    if manifest is None:
        return None, None, None
    slots = (
        ("carrito", settings.waba_first_touch_template_name, "WABA_FIRST_TOUCH_TEMPLATE_NAME"),
        (
            "pago_fallido",
            settings.waba_payment_failure_template_name,
            "WABA_PAYMENT_FAILURE_TEMPLATE_NAME",
        ),
        (
            "precheckout",
            settings.waba_precheckout_template_name,
            "WABA_PRECHECKOUT_TEMPLATE_NAME",
        ),
    )
    parameters: dict[str, tuple[str, ...] | None] = {
        "carrito": None,
        "pago_fallido": None,
        "precheckout": None,
    }
    for slot, configured_name, variable in slots:
        if not manifest.flows[slot]:
            continue
        template = manifest.templates[slot]
        if not configured_name:
            # Sin plantilla propia el pago fallido sale con la del carrito; el
            # arranque ya exige la variable cuando el flag del flujo esta
            # prendido. El primer contacto del formulario no tiene ese
            # prestamo: sin su plantilla no sale nada.
            continue
        if configured_name != template.name:
            raise ValueError(
                f"{variable} must match plantillas.{slot}.nombre of the instance manifest"
            )
        if settings.waba_template_language != template.language:
            raise ValueError(
                f"WABA_TEMPLATE_LANGUAGE must match plantillas.{slot}.idioma "
                "of the instance manifest"
            )
        parameters[slot] = template.parameters
    return parameters["carrito"], parameters["pago_fallido"], parameters["precheckout"]


def _validate_precheckout_first_contact(settings: Settings) -> None:
    """Startup gates of the portable first contact after the landing form.

    The flag makes the form admission also plan a first contact, and the
    dispatcher send it. It only starts when everything that flow needs to go
    out, and everything that has to be able to stop it, is on:

    * an instance manifest (through it, ``flujos.precheckout``, the
      ``intencion`` event and ``[plantillas.precheckout]``) and an entry that
      admits the form;
    * the pilot boundary, the dispatcher and the direct mode (the approved
      template without Hermes), with the scope of this flow, which is another
      one than the recovery scope: a published scope has a single source;
    * its own approved template: there is no fallback to the cart one;
    * what stops it: the purchase from Hotmart (the stop flag and the hottok
      its webhook needs) and the scoped inbound, which carries the durable
      opt-out and the handoff.
    """
    if not settings.portable_precheckout_first_contact_enabled:
        return
    flag = "PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED"
    if settings.instance_manifest is None:
        raise ValueError(f"{flag} requires an instance manifest")
    if not (
        settings.lead_precheckout_enabled or settings.ghl_precheckout_adapter_enabled
    ):
        raise ValueError(
            f"{flag} requires LEAD_PRECHECKOUT_ENABLED or "
            "GHL_PRECHECKOUT_ADAPTER_ENABLED"
        )
    if not settings.pilot_boundary_enabled:
        raise ValueError(f"{flag} requires LANCEMOS_PILOT_BOUNDARY_ENABLED")
    if not settings.dispatcher_enabled:
        raise ValueError(f"{flag} requires DURABLE_DISPATCHER_ENABLED")
    if not settings.dispatcher_approved_template_direct_enabled:
        raise ValueError(
            f"{flag} requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"
        )
    if not settings.pilot_precheckout_scope_key:
        raise ValueError(f"LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY is required for {flag}")
    if (
        settings.pilot_precheckout_scope_version is None
        or settings.pilot_precheckout_scope_version < 1
    ):
        raise ValueError("LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION must be positive")
    if settings.pilot_precheckout_scope_key == settings.pilot_scope_key:
        raise ValueError(
            "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY must differ from "
            "LANCEMOS_PILOT_SCOPE_KEY"
        )
    if not settings.waba_precheckout_template_name:
        raise ValueError(f"WABA_PRECHECKOUT_TEMPLATE_NAME is required for {flag}")
    if not settings.portable_hotmart_purchase_stop_enabled:
        raise ValueError(f"{flag} requires PORTABLE_HOTMART_PURCHASE_STOP_ENABLED")
    if settings.hotmart_hottok is None:
        # Sin el hottok el webhook de Hotmart responde 503 y ninguna compra
        # llega a frenar el primer contacto.
        raise ValueError(f"{flag} requires HOTMART_HOTTOK")
    if not settings.chatwoot_scoped_inbound_senders_enabled:
        raise ValueError(
            f"{flag} requires CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED"
        )


def _waba_template_config(settings: Settings) -> WhatsAppTemplateConfig | None:
    if (
        settings.waba_precheckout_template_name is not None
        and settings.instance_manifest is None
    ):
        # Solo con manifiesto, igual que la categoria del pago fallido: sin
        # manifiesto no hay flujo que la mande.
        raise ValueError(
            "WABA_PRECHECKOUT_TEMPLATE_NAME requires an instance manifest"
        )
    payment_failure_category = settings.waba_payment_failure_template_category
    if payment_failure_category is not None:
        # Solo con manifiesto: Johanna comparte esta configuracion con sus
        # one-shots y no la declara, asi que su categoria no cambia.
        if settings.instance_manifest is None:
            raise ValueError(
                "WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY requires an instance manifest"
            )
        if payment_failure_category not in {"MARKETING", "UTILITY"}:
            raise ValueError(
                "WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY must be MARKETING or UTILITY"
            )
        if not settings.waba_payment_failure_template_name:
            raise ValueError(
                "WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY requires "
                "WABA_PAYMENT_FAILURE_TEMPLATE_NAME"
            )
    if not (
        settings.dispatcher_outbound_enabled
        or settings.johanna_abandonment_one_shot_enabled
        or settings.johanna_abandonment_hotmart_auto_enabled
    ) or settings.pilot_channel_provider != "waba":
        return None
    template_fields = (
        (settings.waba_first_touch_template_name, "WABA_FIRST_TOUCH_TEMPLATE_NAME"),
        (settings.waba_template_language, "WABA_TEMPLATE_LANGUAGE"),
        (settings.waba_template_category, "WABA_TEMPLATE_CATEGORY"),
    )
    for value, name in template_fields:
        if value is None or not value.strip():
            raise ValueError(f"{name} is required for WABA outbound")
    if (
        settings.portable_hotmart_payment_failure_enabled
        and not settings.waba_payment_failure_template_name
    ):
        raise ValueError(
            "WABA_PAYMENT_FAILURE_TEMPLATE_NAME is required for portable "
            "payment failure"
        )
    if settings.waba_template_category not in {"MARKETING", "UTILITY"}:
        raise ValueError("WABA_TEMPLATE_CATEGORY must be MARKETING or UTILITY")
    (
        first_touch_parameters,
        payment_failure_parameters,
        precheckout_parameters,
    ) = _manifest_template_parameters(settings)
    # La plantilla del primer contacto del formulario entra solo con su flag.
    # Apagado, una accion precheckout_intent que hubiera quedado planificada
    # no encuentra plantilla y cierra sin mandar nada: nunca usa la del carrito.
    precheckout_name = (
        settings.waba_precheckout_template_name
        if settings.portable_precheckout_first_contact_enabled
        else None
    )
    return WhatsAppTemplateConfig(
        first_touch_name=settings.waba_first_touch_template_name,  # type: ignore[arg-type]
        followup_name=settings.waba_followup_template_name,  # type: ignore[arg-type]
        language=settings.waba_template_language,  # type: ignore[arg-type]
        category=settings.waba_template_category,  # type: ignore[arg-type]
        first_touch_parameter="buyer_name_and_product",
        payment_failure_name=settings.waba_payment_failure_template_name,
        first_touch_body_parameters=first_touch_parameters,
        payment_failure_body_parameters=payment_failure_parameters,
        payment_failure_category=payment_failure_category,
        precheckout_name=precheckout_name,
        precheckout_body_parameters=(
            precheckout_parameters if precheckout_name is not None else None
        ),
    )


def _validate_approved_template_direct(
    settings: Settings,
    *,
    waba_template: WhatsAppTemplateConfig | None,
    portable_dynamic_recipient: bool,
    portable_runtime: bool,
) -> None:
    """Startup gates of the direct mode and of the final Meta effect with a manifest.

    The direct mode only exists for an instance manifest with the durable WABA
    outbound and a portable recovery flow. With a manifest, the final Meta
    effect of the durable outbound requires it: in the Hermes mode the draft
    never reaches Meta and the shared SOUL forbids the agent from writing
    first, so there is nothing valid to send
    (docs/contracts/approved-template-direct-dispatch-v1.md).

    In a portable runtime the first-name greeting only reaches the direct
    dispatcher (Johanna's one-shots, its other consumer, are not portable), so
    without the direct mode the flag would be accepted and do nothing. It is
    refused instead, the same way the dispatcher constructor refuses it.
    """
    if (
        portable_runtime
        and settings.lead_first_name_greeting_enabled
        and not settings.dispatcher_approved_template_direct_enabled
    ):
        raise ValueError(
            "LEAD_FIRST_NAME_GREETING_ENABLED in a portable runtime requires "
            "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"
        )
    if settings.dispatcher_approved_template_direct_enabled:
        if settings.instance_manifest is None:
            raise ValueError(
                "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED requires an instance manifest"
            )
        if not settings.dispatcher_outbound_enabled:
            raise ValueError(
                "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED requires "
                "DURABLE_OUTBOUND_ENABLED"
            )
        if settings.pilot_channel_provider != "waba" or waba_template is None:
            raise ValueError(
                "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED requires the waba "
                "provider and its approved templates"
            )
        if not portable_dynamic_recipient:
            raise ValueError(
                "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED requires a portable "
                "recovery flow"
            )
    if (
        settings.instance_manifest is not None
        and settings.meta_final_effect_enabled
        and settings.dispatcher_outbound_enabled
        and not settings.dispatcher_approved_template_direct_enabled
    ):
        raise ValueError(
            "META_FINAL_EFFECT_ENABLED with DURABLE_OUTBOUND_ENABLED and an "
            "instance manifest requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"
        )


class _PhoneEquivalentCheckoutIssuance:
    """El cliente que recibe deliver_checkout_issuance_v2 con manifiesto.

    La reserva va a reserve_portable_checkout_issuance_v2, que busca la
    intencion del movil y el opt-out por las dos formas del telefono: quien
    dejo el formulario con 52... (54...) y escribe desde su wa_id 521...
    (549...) recibe el enlace con la oferta, el sck y la intencion del
    formulario, y quien ya compro no recibe otro. Autorizar y finalizar
    trabajan por issuance_id y no cambian. Sin manifiesto (Johanna) el
    bridge le pasa el cliente tal cual y la reserva sigue siendo la
    compartida.
    """

    def __init__(self, client: object) -> None:
        self._client = client

    async def reserve_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> object:
        return await self._client.reserve_chatwoot_checkout_issuance_v2(  # type: ignore[attr-defined]
            phone_equivalence=True, **kwargs
        )

    async def authorize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> object:
        return await self._client.authorize_chatwoot_checkout_issuance_v2(  # type: ignore[attr-defined]
            **kwargs
        )

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> object:
        return await self._client.finalize_chatwoot_checkout_issuance_v2(  # type: ignore[attr-defined]
            **kwargs
        )


GHL_PRECHECKOUT_ADAPTER_PATH = "/webhooks/adapters/ghl/lead-precheckout"
_GHL_LOGGABLE_FORM_ID = re.compile(r"[A-Za-z0-9]{20}")


@dataclass
class _GhlAdapterTrace:
    """The one log line of a GHL adapter request.

    docs/contracts/ghl-precheckout-adapter-v1.md, "Logs": outcome, reason, form
    id, landing, offer, delivery id, phone region and whether UTM or fbclid
    came. Never the name, email, phone, IP, userAgent, contact_id, fbclid,
    fbEventId, the URL query, the token or the body.
    """

    form: str = "-"
    landing: str = "-"
    offer: str = "-"
    delivery_id: str = "-"
    phone_region: str = "-"
    has_utm: str = "-"
    has_fbclid: str = "-"
    # Por que no se leyo el cuerpo de un pedido sin header, que se responde 401.
    unauthenticated_reason: str | None = None

    def note_form(self, body: dict[str, object]) -> None:
        # The form id is public (it is in the widget URL). Anything that does
        # not have its shape is not logged: the value comes from the body.
        submission = body.get("attributionSource")
        form_id = submission.get("mediumId") if isinstance(submission, dict) else None
        if isinstance(form_id, str) and _GHL_LOGGABLE_FORM_ID.fullmatch(form_id):
            self.form = form_id

    def emit(self, *, outcome: str, status_code: int, reason: str) -> None:
        # Un envio que no quedo admitido sale como warning: el bridge no
        # configura logging y bajo uvicorn solo los warnings llegan a la salida
        # del contenedor. El adaptador apagado es configuracion, no un envio
        # perdido.
        not_admitted = (
            status_code != 200 and reason != "ghl_precheckout_adapter_not_enabled"
        )
        if self.unauthenticated_reason is not None:
            reason = f"{reason}/{self.unauthenticated_reason}"
        logger.log(
            logging.WARNING if not_admitted else logging.INFO,
            "ghl_precheckout_adapter outcome=%s status=%s reason=%s form=%s "
            "landing=%s offer=%s delivery_id=%s phone_region=%s has_utm=%s "
            "has_fbclid=%s",
            outcome,
            status_code,
            reason,
            self.form,
            self.landing,
            self.offer,
            self.delivery_id,
            self.phone_region,
            self.has_utm,
            self.has_fbclid,
        )


def _ghl_adapter_risk_readiness(manifest: InstanceManifest) -> str | None:
    """The ``ghl_adapter_risk`` value of /ready, or None when it does not apply.

    Never the name of who accepted: /ready carries no personal data. The name
    goes to the startup log only.
    """
    state = manifest.ghl_adapter_risk
    acceptance = manifest.ghl_risk_acceptance
    if state == "accepted" and acceptance is not None:
        return f"accepted:{acceptance.accepted_on.isoformat()}:{acceptance.contract}"
    return state


def _log_ghl_adapter_risk(manifest: InstanceManifest) -> None:
    # Un warning y no un info: el bridge no configura logging y bajo uvicorn
    # solo los warnings llegan a la salida del contenedor. Una vez por arranque.
    state = manifest.ghl_adapter_risk
    acceptance = manifest.ghl_risk_acceptance
    if state == "accepted" and acceptance is not None:
        logger.warning(
            "ghl_adapter_risk acceptance=accepted by=%s on=%s contract=%s",
            acceptance.accepted_by,
            acceptance.accepted_on.isoformat(),
            acceptance.contract,
        )
    elif state == "not_accepted":
        logger.warning(
            "ghl_adapter_risk acceptance=absent gated=%s,consented_audience",
            ",".join(GHL_RISK_GATED_FLOWS),
        )
    elif state == "no_adapter_section":
        # Sin la seccion no hay guarda: si la instancia uso el adaptador, sus
        # intenciones siguen en la base y estos flujos las tratan como las de
        # una landing.
        logger.warning(
            "ghl_adapter_risk acceptance=no_adapter_section flows=%s "
            "detail=intents_admitted_by_a_removed_adapter_are_not_gated",
            ",".join(flow for flow in GHL_RISK_GATED_FLOWS if manifest.flows[flow]),
        )


async def _ghl_adapter_audience_block(
    supabase: SupabaseClient | None, pilot_boundary: PilotBoundaryConfig
) -> str | None:
    """Why this pilot scope cannot run with ``[adaptadores.ghl]`` unaccepted.

    Only asked when the manifest has the adapter section, no written risk
    acceptance and the pilot boundary on. An audience with consent
    (``consented_intent`` or ``consented_intent_in_cohort``) uses the intent of
    the form as the audience, and the base cannot tell an adapter intent from a
    landing one. Fails closed: a read that fails, a scope that is not
    published or an unknown mode block too. ``None`` only for
    ``manual_cohort``.
    """
    if supabase is None:
        return "ghl_adapter_risk_audience_unavailable"
    try:
        mode = await supabase.get_pilot_scope_audience_mode(
            pilot_boundary=pilot_boundary
        )
    except Exception as exc:
        logger.warning(
            "ghl_adapter_risk_audience_check_failed error_type=%s",
            type(exc).__name__,
        )
        return "ghl_adapter_risk_audience_unavailable"
    if mode in PILOT_SCOPE_CONSENTED_AUDIENCE_MODES:
        return "ghl_adapter_risk_not_accepted"
    if mode != "manual_cohort":
        return "ghl_adapter_risk_audience_unavailable"
    return None


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
    slack_runtime: SlackBridgeRuntime | None = None,
    lead_first_name_client: FirstNameInferenceClient | None = None,
    audio_transcriber: AudioTranscriber | None = None,
) -> FastAPI:
    boolean_fields = [
        field
        for field in dataclass_fields(settings)
        if field.type in (bool, "bool")
    ]
    invalid_boolean_fields = sorted(
        field.name
        for field in boolean_fields
        if type(getattr(settings, field.name)) is not bool
    )
    if invalid_boolean_fields:
        raise ValueError(
            "Settings boolean fields must be bool: "
            + ", ".join(invalid_boolean_fields)
        )
    payment_link_config = PaymentLinkConfig(
        tracking_fields=settings.payment_link_tracking_fields,
        tracking_prefix=settings.payment_link_tracking_prefix,
        max_age_seconds=settings.payment_link_max_age_seconds,
    )
    if settings.payment_link_enabled and not all((
        settings.chatwoot_cut_b_admission_enabled,
        settings.chatwoot_cut_b_agent_enabled,
        settings.automated_replies_enabled,
        settings.chatwoot_scoped_inbound_senders_enabled,
    )):
        raise ValueError(
            "PAYMENT_LINK_ENABLED requires scoped Cut B admission, agent and replies"
        )
    explicit_manifest_runtime = settings.commercial_ally_manifest_path is not None
    if settings.instance_manifest is not None:
        _validate_instance_manifest_gates(settings)
    elif settings.commercial_knowledge is not None:
        raise ValueError("commercial knowledge requires an instance manifest")
    medication_subject_re = _MEDICATION_GUIDANCE_SUBJECT_RE
    medication_action_re = _MEDICATION_GUIDANCE_ACTION_RE
    instance_readiness: dict[str, str] = {}
    if settings.instance_manifest is not None:
        medication_subject_re = _stem_pattern(
            settings.instance_manifest.sensitive_subjects
        )
        medication_action_re = _stem_pattern(
            settings.instance_manifest.sensitive_actions
        )
        instance_readiness = {
            "instance_ally": settings.instance_manifest.ally_ref,
            "instance_product_version": settings.instance_manifest.product_version,
        }
        if settings.ghl_precheckout_adapter_enabled:
            # Cuantos formularios admite, sin exponer sus ids. Solo con el
            # flag prendido: apagado, el payload de /ready no cambia.
            instance_readiness["ghl_precheckout_adapter"] = (
                f"enabled:{len(settings.instance_manifest.ghl_form_ids)}-forms"
            )
        # El riesgo del adaptador cuelga de [adaptadores.ghl] y no del flag
        # (las intenciones que admitio siguen en la base con el flag apagado).
        # La clave aparece solo cuando aplica: sin la seccion y sin un flujo
        # que use intenciones, el payload de /ready no cambia.
        ghl_adapter_risk = _ghl_adapter_risk_readiness(settings.instance_manifest)
        if ghl_adapter_risk is not None:
            instance_readiness["ghl_adapter_risk"] = ghl_adapter_risk
        _log_ghl_adapter_risk(settings.instance_manifest)
    if settings.commercial_knowledge is not None:
        instance_readiness["commercial_knowledge"] = (
            f"v{settings.commercial_knowledge.version}:"
            f"{settings.commercial_knowledge.rendered_sha256}"
        )
    portable_dynamic_recipient = (
        explicit_manifest_runtime
        and (
            settings.portable_hotmart_recovery_enabled
            or settings.portable_hotmart_payment_failure_enabled
            # El primer contacto del formulario tambien le escribe a quien
            # dice la base y no a un JID fijo: sin esto el dispatcher directo
            # no recibe el binding ni arranca.
            or settings.portable_precheckout_first_contact_enabled
        )
    )
    portable_runtime = (
        explicit_manifest_runtime
        or settings.commercial_ally_config != JOHANNA_COMMERCIAL_ALLY
    )
    # Candado del binding v1 de ATT1 (COMMERCIAL_ALLY_CONFIG_PATH con
    # tenant_ref "att1", 2026-09-04). Con el manifiesto v2 no dispara: ahi
    # tenant_ref es "lancemos" y ally_ref "att1". Para ATT1 v2 el efecto final
    # de Meta lo cortan META_FINAL_EFFECT_ENABLED (que con manifiesto exige el
    # modo directo, _validate_approved_template_direct) y, en la base, el
    # estado del piloto, la cohorte y los topes. No se pasa a ally_ref a
    # proposito: dejaria a ATT1 v2 sin poder abrir nunca el efecto final
    # (docs/contracts/approved-template-direct-dispatch-v1.md).
    if (
        settings.commercial_ally_config.tenant_ref == "att1"
        and settings.meta_final_effect_enabled
    ):
        raise ValueError("ATT1 final Meta effect must remain disabled")
    if portable_runtime:
        enabled_unported = sorted(
            field.name
            for field in boolean_fields
            if getattr(settings, field.name) is True
            and (
                not explicit_manifest_runtime
                or field.name not in PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
            )
        )
        if settings.hotmart_hottok is not None and not (
            explicit_manifest_runtime
            and settings.portable_hotmart_purchase_stop_enabled
        ):
            enabled_unported.append("hotmart_hottok")
        if enabled_unported:
            raise ValueError(
                "ATT1 runtime capabilities are not portable: "
                + ", ".join(enabled_unported)
            )
    if settings.chatwoot_scoped_inbound_senders_enabled and (
        settings.chatwoot_account_id
        != settings.commercial_ally_config.chatwoot_account_id
        or settings.chatwoot_inbox_id
        != settings.commercial_ally_config.chatwoot_inbox_id
        or settings.chatwoot_cut_b_scope_key
        != settings.commercial_ally_config.inbound_scope_key
        or settings.chatwoot_cut_b_scope_version
        != settings.commercial_ally_config.inbound_scope_version
    ):
        raise ValueError(
            "CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED must match commercial ally "
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
    if settings.chatwoot_post_inbound_discount_planning_enabled:
        if settings.chatwoot_cut_b_agent_enabled:
            raise ValueError(
                "post-inbound discount planning cannot enable the Cut B agent"
            )
        if not explicit_manifest_runtime:
            raise ValueError(
                "post-inbound discount planning requires an explicit commercial "
                "ally manifest"
            )
        if (
            settings.instance_manifest is None
            and settings.commercial_ally_config.tenant_ref != "att1"
        ):
            raise ValueError(
                "post-inbound discount planning is restricted to ATT1"
            )
        if not all((
            settings.chatwoot_cut_b_admission_enabled,
            settings.supabase_base_url,
            settings.supabase_service_role_key,
            settings.commercial_ally_discount_policy_key,
            settings.commercial_ally_discount_policy_version,
        )):
            raise ValueError(
                "post-inbound discount planning requires Cut B, Supabase, and an "
                "exact discount policy"
            )
        if settings.commercial_ally_discount_policy_version is None or (
            settings.commercial_ally_discount_policy_version < 1
        ):
            raise ValueError(
                "COMMERCIAL_ALLY_DISCOUNT_POLICY_VERSION must be positive"
            )
        if (
            settings.chatwoot_account_id,
            settings.chatwoot_inbox_id,
            settings.chatwoot_cut_b_scope_key,
            settings.chatwoot_cut_b_scope_version,
        ) != (
            settings.commercial_ally_config.chatwoot_account_id,
            settings.commercial_ally_config.chatwoot_inbox_id,
            settings.commercial_ally_config.inbound_scope_key,
            settings.commercial_ally_config.inbound_scope_version,
        ):
            raise ValueError(
                "post-inbound discount planning must match commercial ally scope"
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
    try:
        validate_actor_prefix(settings.operator_correlation_actor_prefix)
    except InvalidCorrelationResolution as exc:
        raise ValueError(
            "OPERATOR_CORRELATION_ACTOR_PREFIX must be a valid actor ref"
        ) from exc
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
    ) != (
        settings.commercial_ally_config.lead_site,
        settings.commercial_ally_config.lead_landing_id,
        settings.commercial_ally_config.offer_code,
    ):
        raise ValueError("lead precheckout scope must match commercial ally config")
    if settings.ghl_precheckout_adapter_enabled:
        # Todo condicionado al flag: apagado (Johanna, y ATT1 hasta prenderlo)
        # no se evalua nada, ni siquiera un token ausente contra otro secreto
        # ausente.
        if settings.instance_manifest is None:
            raise ValueError(
                "GHL_PRECHECKOUT_ADAPTER_ENABLED requires an instance manifest"
            )
        adapter_token = settings.ghl_precheckout_adapter_token
        if adapter_token is None or len(adapter_token) < 32:
            raise ValueError(
                "GHL_PRECHECKOUT_ADAPTER_TOKEN must contain at least 32 characters"
            )
        # Cualquier usuario de la subcuenta de GHL lee este token en el workflow:
        # igual a otro valor de la configuracion, le daria esa otra autoridad (el
        # token del primer contacto manda mensajes). Se compara contra todos los
        # textos de Settings, no contra una lista de nombres que envejece.
        clashing = sorted(
            field.name
            for field in dataclass_fields(settings)
            if field.name != "ghl_precheckout_adapter_token"
            and isinstance(value := getattr(settings, field.name), str)
            and hmac.compare_digest(
                adapter_token.encode("utf-8", "surrogatepass"),
                value.encode("utf-8", "surrogatepass"),
            )
        )
        if clashing:
            raise ValueError(
                "GHL_PRECHECKOUT_ADAPTER_TOKEN must differ from every other secret "
                "and value of the bridge configuration; it equals "
                + ", ".join(clashing)
            )
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
    _validate_precheckout_first_contact(settings)
    waba_template = _waba_template_config(settings)
    _validate_approved_template_direct(
        settings,
        waba_template=waba_template,
        portable_dynamic_recipient=portable_dynamic_recipient,
        portable_runtime=portable_runtime,
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
    # Con [adaptadores.ghl] y sin la aceptacion escrita del riesgo, el scope
    # del piloto no puede tener una audiencia con consentimiento: se lee de la
    # base en el arranque (lifespan, antes de cualquier worker) y en /ready.
    # Con la aceptacion, o sin la seccion, no se lee nada.
    ghl_adapter_audience_gate = (
        pilot_boundary is not None
        and settings.instance_manifest is not None
        and bool(settings.instance_manifest.ghl_form_ids)
        and settings.instance_manifest.ghl_risk_acceptance is None
    )
    # El scope del primer contacto del formulario: otra clave y otra version,
    # el mismo tenant y el mismo canal que el de recuperacion. Solo lo lee
    # /ready; el envio resuelve el scope por el binding del caso.
    precheckout_pilot_boundary = (
        PilotBoundaryConfig(
            scope_key=settings.pilot_precheckout_scope_key,  # type: ignore[arg-type]
            scope_version=settings.pilot_precheckout_scope_version,  # type: ignore[arg-type]
            tenant_key=settings.pilot_tenant_key,  # type: ignore[arg-type]
            channel_provider=settings.pilot_channel_provider,  # type: ignore[arg-type]
            channel_account_ref=settings.pilot_channel_account_ref,  # type: ignore[arg-type]
        )
        if settings.portable_precheckout_first_contact_enabled
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
    stalled_numeric_values = (
        settings.chatwoot_stalled_monitor_interval_seconds,
        settings.chatwoot_stalled_after_seconds,
        settings.chatwoot_stalled_max_age_seconds,
        settings.chatwoot_stalled_recovery_cooldown_seconds,
    )
    if (
        not all(math.isfinite(value) for value in stalled_numeric_values)
        or settings.chatwoot_stalled_monitor_interval_seconds <= 0
        or settings.chatwoot_stalled_after_seconds < 0
        or settings.chatwoot_stalled_max_age_seconds
        < settings.chatwoot_stalled_after_seconds
        or settings.chatwoot_stalled_recovery_cooldown_seconds <= 0
        or not 1 <= settings.chatwoot_stalled_max_pages <= 20
        or settings.chatwoot_stalled_max_recovery_admissions < 1
    ):
        raise ValueError("invalid stalled conversation monitor configuration")
    if (
        not math.isfinite(settings.conversation_reactivation_interval_seconds)
        or settings.conversation_reactivation_interval_seconds <= 0
        or settings.conversation_reactivation_min_age_seconds < 0
        or settings.conversation_reactivation_max_age_seconds
        < settings.conversation_reactivation_min_age_seconds
        or settings.conversation_reactivation_max < 1
        or settings.conversation_reactivation_max_sends_per_scan < 1
        or not 1 <= settings.conversation_reactivation_max_pages <= 20
    ):
        raise ValueError("invalid conversation reactivation configuration")
    # Mandar una plantilla de marketing es un efecto externo irreversible:
    # sin plantilla declarada, sin inbox canonico y sin el AgentBot que la
    # emite, el barredor no arranca en vez de arrancar a medias.
    if settings.conversation_reactivation_enabled and (
        settings.conversation_reactivation_template_name is None
        or settings.chatwoot_account_id is None
        or settings.chatwoot_account_id < 1
        or settings.chatwoot_inbox_id is None
        or settings.chatwoot_inbox_id < 1
        or settings.chatwoot_agent_bot_access_token is None
        or settings.agent_bot_id is None
        or (
            settings.allowed_jid is None
            and not settings.chatwoot_scoped_inbound_senders_enabled
        )
    ):
        raise ValueError(
            "CONVERSATION_REACTIVATION_ENABLED requires WABA_REACTIVATION_"
            "TEMPLATE_NAME, canonical Chatwoot ids, the agent bot and a "
            "bounded sender scope"
        )
    if (
        not math.isfinite(settings.conversation_followup_interval_seconds)
        or settings.conversation_followup_interval_seconds <= 0
        or settings.conversation_followup_min_age_seconds < 0
        or settings.conversation_followup_max_age_seconds
        < settings.conversation_followup_min_age_seconds
        or settings.conversation_followup_max_sends_per_scan < 1
        or not 1 <= settings.conversation_followup_max_pages <= 20
    ):
        raise ValueError("invalid conversation followup configuration")
    # Mandar un cupon es un efecto externo irreversible: sin plantilla, sin
    # cupon, sin producto, sin el AgentBot que la emite y sin la admision de
    # Corte B (el link sale del caso comercial que abre esa admision), el
    # barredor no arranca en vez de arrancar a medias.
    if settings.conversation_followup_enabled and (
        settings.conversation_followup_template_name is None
        or settings.conversation_followup_coupon_code is None
        or not COUPON_CODE_RE.fullmatch(settings.conversation_followup_coupon_code)
        or settings.conversation_followup_product_name is None
        or settings.chatwoot_account_id is None
        or settings.chatwoot_account_id < 1
        or settings.chatwoot_inbox_id is None
        or settings.chatwoot_inbox_id < 1
        or settings.chatwoot_agent_bot_access_token is None
        or settings.agent_bot_id is None
        or not settings.chatwoot_cut_b_admission_enabled
        or settings.supabase_base_url is None
        or settings.supabase_service_role_key is None
        or (
            settings.allowed_jid is None
            and not settings.chatwoot_scoped_inbound_senders_enabled
        )
    ):
        raise ValueError(
            "CONVERSATION_FOLLOWUP_ENABLED requires CONVERSATION_FOLLOWUP_"
            "TEMPLATE_NAME, a valid CONVERSATION_FOLLOWUP_COUPON_CODE, "
            "CONVERSATION_FOLLOWUP_PRODUCT_NAME, canonical Chatwoot ids, the "
            "agent bot, Cut B admission, Supabase and a bounded sender scope"
        )
    if settings.chatwoot_stalled_monitor_enabled and (
        not settings.chatwoot_cut_b_admission_enabled
        or not settings.chatwoot_cut_b_agent_enabled
        or not settings.automated_replies_enabled
        or settings.chatwoot_account_id is None
        or settings.chatwoot_account_id < 1
        or settings.chatwoot_inbox_id is None
        or settings.chatwoot_inbox_id < 1
        or (
            settings.allowed_jid is None
            and not settings.chatwoot_scoped_inbound_senders_enabled
        )
    ):
        raise ValueError(
            "CHATWOOT_STALLED_MONITOR_ENABLED requires active scoped Chatwoot replies"
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
            resume_macro_id=settings.chatwoot_resume_macro_id,
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
    # La procedencia del prompt se anota si hay con que: el turno del agente
    # no depende de esto, pero sin esto el feedback no se puede atribuir a
    # una version del agente.
    if shared_supabase is not None and hasattr(
        shadow_processor, "bind_provenance_recorder"
    ):
        shadow_processor.bind_provenance_recorder(
            SupabaseTurnProvenanceRecorder(
                client=shared_supabase,
                tenant_ref=settings.agent_provenance_tenant_ref,
                scope_ref=settings.agent_provenance_scope_ref,
                bridge_release=settings.bridge_release,
            )
        )
    if (
        settings.operator_correlation_read_enabled
        or settings.operator_correlation_write_enabled
    ) and shared_supabase is None:
        raise ValueError("operator correlation access requires Supabase")
    if not settings.chatwoot_audio_transcription_enabled:
        audio_transcriber = None
    elif audio_transcriber is None:
        # El audio se baja del mismo host que la API de Chatwoot: data_url es
        # una URL firmada de Active Storage en ese host (medido el 2026-09-28).
        media_host = (
            urlparse(settings.chatwoot_base_url).hostname
            if settings.chatwoot_base_url
            else None
        )
        if settings.openrouter_api_key is None or not media_host:
            raise ValueError("audio_transcription_configuration_incomplete")
        audio_transcriber = AudioTranscriber(
            api_key=settings.openrouter_api_key,
            models=settings.audio_transcription_models,
            media_host=media_host,
            cache_dir=settings.audio_transcription_cache_dir,
        )
    if not settings.lead_first_name_inference_enabled:
        lead_first_name_client = None
    elif lead_first_name_client is None:
        if (
            shared_supabase is None
            or settings.hermes_api_base_url is None
            or settings.hermes_api_key is None
            or settings.lead_first_name_model_name is None
        ):
            raise ValueError("lead_first_name_configuration_incomplete")
        lead_first_name_client = FirstNameInferenceClient(
            base_url=settings.hermes_api_base_url,
            api_key=settings.hermes_api_key,
            model_name=settings.lead_first_name_model_name,
        )
    correlation_preresolution_worker: CorrelationPreresolutionWorker | None = None
    if settings.correlation_preresolution_enabled:
        if (
            shared_supabase is None
            or settings.hermes_api_base_url is None
            or settings.hermes_api_key is None
            or settings.correlation_preresolution_model_name is None
            or settings.correlation_preresolution_worker_id is None
        ):
            raise ValueError("correlation_preresolution_configuration_incomplete")
        correlation_preresolution_worker = CorrelationPreresolutionWorker(
            store=shared_supabase,
            model=CorrelationPreresolutionClient(
                base_url=settings.hermes_api_base_url,
                api_key=settings.hermes_api_key,
                model_name=settings.correlation_preresolution_model_name,
                prompt_version=settings.correlation_preresolution_prompt_version,
            ),
            tenant_ref=settings.commercial_ally_config.tenant_ref,
            funnel_ref=settings.commercial_ally_config.funnel_ref,
            worker_id=settings.correlation_preresolution_worker_id,
            poll_interval_seconds=(
                settings.correlation_preresolution_poll_interval_seconds
            ),
        )
    if (
        settings.slack_connector_projection_enabled
        and correlation_preresolution_worker is None
    ):
        raise ValueError(
            "Slack projection requires correlation pre-resolution"
        )
    slack_runtime_owned = False
    connector_url_configured = settings.slack_connector_base_url is not None
    connector_token_configured = settings.slack_connector_bearer_token is not None
    if connector_url_configured != connector_token_configured:
        raise ValueError("slack_connector_configuration_incomplete")
    if settings.slack_connector_projection_enabled:
        if shared_supabase is None:
            raise ValueError("Slack projection requires Supabase")
        if settings.slack_connector_worker_id is None:
            raise ValueError("SLACK_CONNECTOR_WORKER_ID is required")
        if slack_runtime is None:
            slack_runtime = create_slack_bridge_runtime(
                base_url=settings.slack_connector_base_url,
                bearer_token=settings.slack_connector_bearer_token,
                expected_tenant_ref=settings.commercial_ally_config.ally_ref,
            )
            if slack_runtime is None:
                raise ValueError("Slack projection requires connector configuration")
            slack_runtime_owned = True
    slack_projection_worker: SlackCorrelationProjectionWorker | None = None
    if settings.slack_connector_projection_enabled:
        assert shared_supabase is not None
        assert slack_runtime is not None
        assert settings.slack_connector_worker_id is not None
        slack_projection_worker = SlackCorrelationProjectionWorker(
            store=shared_supabase,
            producer=slack_runtime.producer,
            tenant_ref=settings.commercial_ally_config.tenant_ref,
            funnel_ref=settings.commercial_ally_config.funnel_ref,
            worker_id=settings.slack_connector_worker_id,
            poll_interval_seconds=settings.slack_connector_poll_interval_seconds,
            batch_size=settings.slack_connector_batch_size,
            lease_seconds=settings.slack_connector_lease_seconds,
            binding_version=(
                settings.commercial_ally_config.binding_version
                if portable_runtime
                else None
            ),
        )
    # El aviso de cada derivacion a humano (HND-001) comparte el productor del
    # conector con las correlaciones y vive detras de dos flags que ya estan en
    # true en produccion: el del conector (crea el productor) y el de la
    # proyeccion del handoff (sin el no hay derivaciones que avisar). A
    # proposito no entra en /ready: ver la nota en SlackHandoffProjectionWorker.
    slack_handoff_projection_worker: SlackHandoffProjectionWorker | None = None
    if (
        settings.slack_connector_projection_enabled
        and settings.human_handoff_projection_enabled
    ):
        assert shared_supabase is not None
        assert slack_runtime is not None
        assert settings.slack_connector_worker_id is not None
        slack_handoff_projection_worker = SlackHandoffProjectionWorker(
            store=shared_supabase,
            producer=slack_runtime.producer,
            worker_id=f"{settings.slack_connector_worker_id}:handoff",
            poll_interval_seconds=settings.slack_connector_poll_interval_seconds,
            batch_size=settings.slack_connector_batch_size,
            lease_seconds=settings.slack_connector_lease_seconds,
        )
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
                        first_touch_parameter="buyer_name_and_product",
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
        # In the direct mode the dispatcher never asks Hermes, so it never
        # receives a handoff suggestion and does not consume the admission.
        dispatcher_handoff_enabled = (
            settings.dispatcher_enabled
            and settings.dispatcher_outbound_enabled
            and settings.pilot_boundary_enabled
            and not settings.dispatcher_approved_template_direct_enabled
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
            precheckout_outbound_enabled=(
                settings.precheckout_delayed_outbound_enabled
            ),
            lead_first_name_greeting_enabled=(
                settings.lead_first_name_greeting_enabled
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
        if portable_dynamic_recipient and (
            settings.pilot_tenant_key
            != settings.commercial_ally_config.tenant_ref
            or settings.pilot_channel_provider != "waba"
            or settings.chatwoot_account_id
            != settings.commercial_ally_config.chatwoot_account_id
            or settings.chatwoot_inbox_id
            != settings.commercial_ally_config.chatwoot_inbox_id
            or settings.pilot_channel_account_ref
            != f"chatwoot-inbox:{settings.commercial_ally_config.chatwoot_inbox_id}"
        ):
            raise ValueError(
                "portable resolution worker requires exact ally pilot and Chatwoot scope"
            )
        if settings.allowed_jid is None and not portable_dynamic_recipient:
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
                dynamic_recipient_enabled=portable_dynamic_recipient,
                template=waba_template,
                whatsapp_equivalence_enabled=portable_dynamic_recipient,
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
            payment_failure_enabled=(
                settings.portable_hotmart_payment_failure_enabled
            ),
            pilot_boundary=pilot_boundary,
            commercial_ally_config=(
                settings.commercial_ally_config
                if portable_dynamic_recipient
                else None
            ),
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
        approved_template_direct = settings.dispatcher_approved_template_direct_enabled
        if settings.dispatcher_outbound_enabled:
            # In the direct mode the dispatcher sends the approved template and
            # never asks Hermes for a draft, so Hermes is not a dependency.
            outbound_agent = None if approved_template_direct else recovery_agent_client
            if (
                not approved_template_direct
                and outbound_agent is None
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
                and (
                    settings.allowed_jid is not None
                    or portable_dynamic_recipient
                )
                and isinstance(control_client, ChatwootClient)
            ):
                outbound_sender = ChatwootMessageSender(
                    chatwoot=control_client,
                    inbox_id=settings.chatwoot_inbox_id,
                    allowed_jid=settings.allowed_jid,
                    dynamic_recipient_enabled=portable_dynamic_recipient,
                    template=waba_template,
                    whatsapp_equivalence_enabled=portable_dynamic_recipient,
                )
            if outbound_sender is None or (
                outbound_agent is None and not approved_template_direct
            ):
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
            commercial_ally_config=(
                settings.commercial_ally_config
                if portable_dynamic_recipient
                else None
            ),
            portable_recipient_enabled=portable_dynamic_recipient,
            pilot_boundary=pilot_boundary,
            chatwoot_inbox_id=settings.chatwoot_inbox_id,
            human_handoff_admission_enabled=(
                settings.human_handoff_admission_enabled
                and not approved_template_direct
            ),
            handoff_projection_policy_key=settings.handoff_projection_policy_key,
            handoff_projection_policy_version=(
                settings.handoff_projection_policy_version
            ),
            final_meta_effect_gate=(
                FinalMetaEffectGate(
                    enabled=settings.meta_final_effect_enabled,
                    evidence_dir=settings.meta_final_effect_evidence_dir,
                )
                if settings.dispatcher_outbound_enabled
                and settings.pilot_channel_provider == "waba"
                else None
            ),
            waba_template=waba_template,
            approved_template_direct=approved_template_direct,
            lead_first_name_greeting_enabled=(
                settings.lead_first_name_greeting_enabled
                and approved_template_direct
            ),
            # Solo el runtime portable trata 52/521 y 54/549 como el mismo
            # telefono, y solo con un sender que sabe resolver el destinatario
            # en Chatwoot antes del gate final (el que el bridge arma arriba).
            # Con cualquier otro sender el dispatcher sigue mandando al
            # telefono exacto del contacto.
            whatsapp_equivalence_enabled=(
                portable_dynamic_recipient
                and isinstance(outbound_sender, ChatwootMessageSender)
                and outbound_sender.whatsapp_equivalence_enabled
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
            # Con manifiesto, el opt-out queda guardado con la identidad que la
            # base ya tenia (52…) y la conversacion de Chatwoot es del wa_id
            # (521…): la proyeccion acepta la otra forma del mismo movil.
            whatsapp_equivalence_enabled=settings.instance_manifest is not None,
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
            whatsapp_equivalence_enabled=settings.instance_manifest is not None,
        )

    chatwoot_worker: ChatwootWorker | None = None
    chatwoot_stalled_monitor: ChatwootStalledConversationMonitor | None = None
    conversation_reactivation_sweeper: ConversationReactivationSweeper | None = (
        None
    )
    conversation_followup_sweeper: ConversationFollowupSweeper | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        if ghl_adapter_audience_gate:
            # Antes de arrancar cualquier worker: los workers corren aunque
            # /ready responda 503. Falla cerrado, tambien si la base no
            # responde.
            assert pilot_boundary is not None
            audience_block = await _ghl_adapter_audience_block(
                shared_supabase, pilot_boundary
            )
            if audience_block is not None:
                raise RuntimeError(
                    f"{audience_block}: [adaptadores.ghl] without the written risk "
                    "acceptance cannot run with a consented pilot audience "
                    "(consented_intent, consented_intent_in_cohort), and the "
                    "audience mode of the pilot scope must be readable"
                )
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
            if correlation_preresolution_worker is not None:
                await correlation_preresolution_worker.start()
            if slack_projection_worker is not None:
                if portable_runtime:
                    assert shared_supabase is not None
                    await shared_supabase.resolve_commercial_ally_runtime_binding(
                        settings.commercial_ally_config
                    )
                await slack_projection_worker.start()
            if slack_handoff_projection_worker is not None:
                await slack_handoff_projection_worker.start()
            if chatwoot_worker is not None:
                await chatwoot_worker.start()
            if chatwoot_stalled_monitor is not None:
                await chatwoot_stalled_monitor.start()
            if conversation_reactivation_sweeper is not None:
                await conversation_reactivation_sweeper.start()
            if conversation_followup_sweeper is not None:
                await conversation_followup_sweeper.start()
            yield
        finally:
            for worker_name, worker in (
                (
                    "conversation_followup_sweeper",
                    conversation_followup_sweeper,
                ),
                (
                    "conversation_reactivation_sweeper",
                    conversation_reactivation_sweeper,
                ),
                ("chatwoot_stalled_monitor", chatwoot_stalled_monitor),
                ("chatwoot", chatwoot_worker),
                ("opt_out_projection", opt_out_projection_worker),
                ("human_handoff_projection", human_handoff_projection_worker),
                ("slack_projection", slack_projection_worker),
                ("slack_handoff_projection", slack_handoff_projection_worker),
                ("correlation_preresolution", correlation_preresolution_worker),
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
            if slack_runtime_owned and slack_runtime is not None:
                try:
                    await slack_runtime.aclose()
                except Exception as exc:
                    logger.warning(
                        "slack_runtime_close_failed error_type=%s",
                        type(exc).__name__,
                    )

    app = FastAPI(title="AI Appointment Setter Bridge", lifespan=lifespan)
    app.state.resolution_worker = resolution_worker
    app.state.hotmart_abandonment_timer_worker = hotmart_abandonment_timer_worker
    app.state.durable_dispatcher = durable_dispatcher
    app.state.opt_out_projection_worker = opt_out_projection_worker
    app.state.human_handoff_projection_worker = human_handoff_projection_worker
    app.state.correlation_preresolution_worker = correlation_preresolution_worker
    app.state.slack_projection_worker = slack_projection_worker
    app.state.slack_handoff_projection_worker = slack_handoff_projection_worker
    app.state.chatwoot_inbox = chatwoot_inbox
    app.state.chatwoot_worker = chatwoot_worker
    app.state.chatwoot_stalled_monitor = chatwoot_stalled_monitor
    app.state.conversation_reactivation_sweeper = (
        conversation_reactivation_sweeper
    )
    app.state.conversation_followup_sweeper = conversation_followup_sweeper

    # Con manifiesto, el mismo movil puede estar guardado con la otra forma
    # (52 + 10 del formulario, 521 + 10 del wa_id; 54 y 549 en Argentina). Sin
    # manifiesto (Johanna) nada de esto corre: el wa_id se usa textual.
    whatsapp_inbound_equivalence = settings.instance_manifest is not None

    async def admit_inbound_commercial_case(
        *,
        scope_key: str,
        scope_version: int,
        external_conversation_id: int,
        external_user_id: str,
    ) -> InboundCommercialCaseAdmissionResult:
        """La admision entrante que corresponde a este runtime.

        Con manifiesto (ATT1) va por admit_portable_inbound_commercial_case_v1:
        quien responde a una plantilla del piloto escribe en la conversacion
        que la aceptacion del envio dejo en ``enabled``, y la v2 sola la
        rechaza (22000 ``inbound_canonical_conversation_conflict``). La
        portable adopta esa conversacion y despues delega en la v2. Sin
        manifiesto (Johanna), la v2 de siempre con los mismos argumentos.
        Las tres llamadas (admision, reautorizacion y readmision tras
        reanudar) pasan por aca, asi un runtime habla con una sola RPC.
        """
        assert shared_supabase is not None
        if whatsapp_inbound_equivalence:
            return await shared_supabase.admit_portable_inbound_commercial_case(
                scope_key=scope_key,
                scope_version=scope_version,
                external_conversation_id=external_conversation_id,
                external_user_id=external_user_id,
            )
        return await shared_supabase.admit_inbound_commercial_case(
            scope_key=scope_key,
            scope_version=scope_version,
            external_conversation_id=external_conversation_id,
            external_user_id=external_user_id,
        )

    async def resolve_inbound_external_user_id(
        wa_id: str,
        *,
        conversation_id: object,
    ) -> str:
        """El external_user_id con que la base ya conoce a quien escribe.

        Si entre las formas equivalentes del wa_id hay exactamente una
        identidad activa del inbox y no es la textual, se usa la guardada: asi
        el opt-out, la admision y el enlace caen en el contacto que ya existe
        en vez de abrir otro. Con mas de una se usa la textual y queda un
        warning con ids de Chatwoot y region, nunca el numero. Sin ninguna, la
        textual.
        """
        if (
            not whatsapp_inbound_equivalence
            or shared_supabase is None
            or settings.chatwoot_account_id is None
            or settings.chatwoot_inbox_id is None
        ):
            return wa_id
        forms = equivalent_whatsapp_phones(wa_id)
        if len(forms) < 2:
            return wa_id
        try:
            identities = await shared_supabase.find_active_whatsapp_identities(
                chatwoot_account_id=settings.chatwoot_account_id,
                chatwoot_inbox_id=settings.chatwoot_inbox_id,
                external_user_ids=forms,
            )
        except SupabaseError as exc:
            raise RetryableChatwootWorkError(
                "chatwoot_inbound_identity_lookup_failed"
            ) from exc
        stored = sorted({identity.external_user_id for identity in identities})
        if len(stored) == 1 and stored[0] != wa_id:
            logger.info(
                "chatwoot_inbound_identity_equivalent conversation=%s region=%s",
                conversation_id,
                whatsapp_phone_region(wa_id),
            )
            return stored[0]
        if len(stored) > 1:
            logger.warning(
                "chatwoot_inbound_identity_duplicated account=%s inbox=%s "
                "conversation=%s region=%s identities=%s",
                settings.chatwoot_account_id,
                settings.chatwoot_inbox_id,
                conversation_id,
                whatsapp_phone_region(wa_id),
                len(stored),
            )
        return wa_id

    def inbound_opt_out_stop_user_ids(
        wa_id: str,
        external_user_id: str,
    ) -> tuple[str, ...]:
        """Los ids cuyo opt-out frena a quien escribe.

        Sin manifiesto, el de siempre. Con manifiesto, el resuelto y despues
        cada forma del wa_id: un opt-out guardado bajo 521… frena igual a
        quien hoy la base conoce como 52….
        """
        if not whatsapp_inbound_equivalence:
            return (external_user_id,)
        return tuple(
            dict.fromkeys(
                (external_user_id, wa_id, *equivalent_whatsapp_phones(wa_id))
            )
        )

    async def run_shadow_with_canonical_history(
        *,
        delivery_id: str,
        current_message_id: int | None,
        batch_message_ids: tuple[int, ...],
        context: dict[str, object],
        expected_jid: str | None = None,
        opt_out_only: bool = False,
    ) -> CanonicalWorkResult:
        if control_client is None or settings.agent_bot_id is None:
            if opt_out_only:
                return CanonicalWorkResult(proposal=None)
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
        if audio_transcriber is not None:
            # Sin esto, la normalizacion saltea todo mensaje sin texto: un audio
            # desaparece del historial y el agente nunca sabe que existio.
            history = await audio_transcriber.enrich_history(history)
        normalized = _normalize_chatwoot_history(
            history,
            agent_bot_id=settings.agent_bot_id,
            include_team_messages=settings.conversation_resume_enabled,
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
        inbound_wa_id = external_user_id
        external_user_id = await resolve_inbound_external_user_id(
            inbound_wa_id,
            conversation_id=conversation_id,
        )
        if opt_out_enforcement_enabled:
            assert shared_supabase is not None
            assert settings.chatwoot_account_id is not None
            assert settings.chatwoot_inbox_id is not None
            stopped_user_id: str | None = None
            for stop_user_id in inbound_opt_out_stop_user_ids(
                inbound_wa_id, external_user_id
            ):
                try:
                    stopped = await shared_supabase.has_chatwoot_opt_out_stop(
                        chatwoot_account_id=settings.chatwoot_account_id,
                        chatwoot_inbox_id=settings.chatwoot_inbox_id,
                        chatwoot_conversation_id=conversation_id,
                        external_user_id=stop_user_id,
                    )
                except SupabaseError as exc:
                    raise RetryableChatwootWorkError(
                        "chatwoot_opt_out_stop_check_failed"
                    ) from exc
                if stopped:
                    stopped_user_id = stop_user_id
                    break
            if stopped_user_id is not None:
                try:
                    reconciliation = (
                        await shared_supabase.reconcile_chatwoot_opt_out_stop(
                            chatwoot_account_id=settings.chatwoot_account_id,
                            chatwoot_inbox_id=settings.chatwoot_inbox_id,
                            chatwoot_conversation_id=conversation_id,
                            external_user_id=stopped_user_id,
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
        if opt_out_only:
            # Only the stop and the opt-out were asked for: the agent does
            # not run.
            return CanonicalWorkResult(proposal=None)
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
                **_sent_at(message),
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
            # Una persona del equipo acaba de escribir: eso es exactamente
            # atender la derivacion que estaba esperando. Mientras ninguna
            # quede atendida, la reactivacion no manda plantillas y la
            # respuesta del lead no devuelve la conversacion al agente.
            #
            # El momento se toma del reloj del bridge, no del payload: el
            # unico fixture capturado de message_created es de un entrante
            # (chatwoot_paused_lead_reply_inbox_9_conv_177_20260925.json), y
            # un payload saliente de una persona todavia no se capturo. La
            # diferencia es de segundos y la RPC recorta cualquier fecha
            # futura.
            #
            # Falla blanda a proposito: la pausa ya quedo puesta, que es lo
            # que protege al lead. Si la marca no sale, la derivacion sigue
            # contando como pendiente y lo unico que pasa es que nadie
            # reactiva esa conversacion.
            if shared_supabase is not None:
                try:
                    await shared_supabase.mark_human_handoff_attended(
                        external_conversation_id=conversation_id,
                        attended_at=datetime.now(UTC).isoformat(),
                    )
                except Exception:
                    logger.warning(
                        "human_handoff_attendance_failed conversation=%s",
                        conversation_id,
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
        external_user_id = ""

        async def send_scoped_agent_bot_reply(
            *,
            conversation_id: int,
            trigger_message_id: int,
            content: str,
            part_index: int = 1,
            part_count: int = 1,
            prior_parts: tuple[str, ...] = (),
            agent_decision: str | None = None,
            agent_reason_code: str | None = None,
        ) -> dict[str, object]:
            if control_client is None:
                raise RuntimeError("chatwoot_reply_not_configured")
            if (
                durable_reply_authorizer is not None
                and not await durable_reply_authorizer()
            ):
                return {"status": "blocked", "reason": "durable_automation_stop"}
            send_args: dict[str, object] = {
                "conversation_id": conversation_id,
                "trigger_message_id": trigger_message_id,
                "delivery_id": delivery_id,
                "content": content,
                "part_index": part_index,
                "part_count": part_count,
                "prior_parts": prior_parts,
                "expected_inbox_id": settings.chatwoot_inbox_id,
            }
            if agent_decision is not None:
                send_args["agent_decision"] = agent_decision
            if agent_reason_code is not None:
                send_args["agent_reason_code"] = agent_reason_code
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
                reset_wa_id = external_user_id
                external_user_id = await resolve_inbound_external_user_id(
                    reset_wa_id,
                    conversation_id=conversation_id,
                )
                reset_stopped_user_id: str | None = None
                for stop_user_id in inbound_opt_out_stop_user_ids(
                    reset_wa_id, external_user_id
                ):
                    try:
                        stopped = await shared_supabase.has_chatwoot_opt_out_stop(
                            chatwoot_account_id=settings.chatwoot_account_id,
                            chatwoot_inbox_id=settings.chatwoot_inbox_id,
                            chatwoot_conversation_id=conversation_id,
                            external_user_id=stop_user_id,
                        )
                    except SupabaseError as exc:
                        raise RetryableChatwootWorkError(
                            "chatwoot_reset_opt_out_stop_check_failed"
                        ) from exc
                    if stopped:
                        reset_stopped_user_id = stop_user_id
                        break
                if reset_stopped_user_id is not None:
                    try:
                        reconciliation = (
                            await shared_supabase.reconcile_chatwoot_opt_out_stop(
                                chatwoot_account_id=settings.chatwoot_account_id,
                                chatwoot_inbox_id=settings.chatwoot_inbox_id,
                                chatwoot_conversation_id=conversation_id,
                                external_user_id=reset_stopped_user_id,
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
            # Toda llamada a la base de aca en adelante usa la identidad que ya
            # existe para este movil. Lo que se valida contra Chatwoot
            # (scoped_expected_jid) sigue siendo el wa_id textual del webhook.
            external_user_id = await resolve_inbound_external_user_id(
                external_user_id,
                conversation_id=conversation_id,
            )
            if settings.chatwoot_post_inbound_discount_planning_enabled:
                assert settings.commercial_ally_discount_policy_key is not None
                assert settings.commercial_ally_discount_policy_version is not None
                assert settings.chatwoot_account_id is not None
                assert settings.chatwoot_inbox_id is not None
                message_id = payload.get("id")
                created_at = payload.get("created_at")
                if (
                    not isinstance(message_id, int)
                    or isinstance(message_id, bool)
                    or message_id < 1
                    or not isinstance(created_at, (int, float))
                    or isinstance(created_at, bool)
                    or not math.isfinite(created_at)
                    or created_at <= 0
                ):
                    raise RuntimeError(
                        "chatwoot_post_inbound_discount_evidence_invalid"
                    )
                try:
                    inbound_received_at = datetime.fromtimestamp(
                        created_at, tz=UTC
                    ).isoformat()
                    discount_plan = (
                        await shared_supabase.plan_commercial_ally_post_inbound_discount(
                            tenant_ref=settings.commercial_ally_config.tenant_ref,
                            funnel_ref=settings.commercial_ally_config.funnel_ref,
                            binding_version=(
                                settings.commercial_ally_config.binding_version
                            ),
                            discount_policy_key=(
                                settings.commercial_ally_discount_policy_key
                            ),
                            discount_policy_version=(
                                settings.commercial_ally_discount_policy_version
                            ),
                            chatwoot_account_id=settings.chatwoot_account_id,
                            chatwoot_inbox_id=settings.chatwoot_inbox_id,
                            chatwoot_conversation_id=conversation_id,
                            chatwoot_message_id=message_id,
                            external_user_id=external_user_id,
                            inbound_received_at=inbound_received_at,
                        )
                    )
                except (OverflowError, OSError, ValueError) as exc:
                    raise RuntimeError(
                        "chatwoot_post_inbound_discount_evidence_invalid"
                    ) from exc
                except SupabaseError as exc:
                    raise RetryableChatwootWorkError(
                        "chatwoot_post_inbound_discount_planning_failed"
                    ) from exc
                logger.info(
                    "chatwoot_post_inbound_discount_planned outcome=%s",
                    discount_plan.outcome,
                )
                return

            try:
                admission = await admit_inbound_commercial_case(
                    scope_key=settings.chatwoot_cut_b_scope_key,
                    scope_version=settings.chatwoot_cut_b_scope_version,
                    external_conversation_id=conversation_id,
                    external_user_id=external_user_id,
                )
            except SupabaseError as exc:
                if whatsapp_inbound_equivalence:
                    # With a manifest, a "No mas mensajes" does not depend on
                    # the admission. The conversation a recovery template
                    # opened is not a draft-only inbound one, so the admission
                    # rejects every reply to that template: without this the
                    # opt-out of the person who pressed the button was never
                    # recorded and the message was retried without limit.
                    stop_context = _shadow_context(payload)
                    stop_message_id = payload.get("id")
                    if (
                        stop_context is not None
                        and isinstance(stop_message_id, int)
                        and not isinstance(stop_message_id, bool)
                    ):
                        stop_result = await run_shadow_with_canonical_history(
                            delivery_id=delivery_id,
                            current_message_id=stop_message_id,
                            batch_message_ids=batch_message_ids,
                            context=stop_context,
                            expected_jid=scoped_expected_jid,
                            opt_out_only=True,
                        )
                        if stop_result.stopped:
                            return
                    if isinstance(exc, SupabasePermanentError):
                        # Replaying the message cannot change the answer: the
                        # reason goes to the log and the work ends as failed
                        # after the bounded attempts. Nobody answers this
                        # message from here; a person takes it in Chatwoot.
                        logger.warning(
                            "chatwoot_cut_b_admission_rejected "
                            "conversation=%s reason=%s",
                            conversation_id,
                            exc.reason,
                        )
                        raise ChatwootInboundAdmissionRejectedError(
                            "chatwoot_cut_b_admission_rejected"
                        ) from exc
                raise RetryableChatwootWorkError(
                    "chatwoot_cut_b_admission_failed"
                ) from exc

            async def reauthorize_durable_reply() -> bool:
                try:
                    authorization = (
                        await admit_inbound_commercial_case(
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
            if settings.conversation_resume_enabled and isinstance(
                control_client, ChatwootClient
            ):
                # La pausa no es terminal: si el equipo dejo de atender esta
                # conversacion, se levanta.
                #
                # No se condiciona a que la admision haya dado 'blocked'. La
                # pausa tiene dos capas que se escriben por caminos distintos:
                # la etiqueta de Chatwoot la pone cualquier saliente de un
                # `user`, y human_takeover solo lo pone una derivacion. Medido
                # el 2026-09-23 sobre el inbox 9: de 27 conversaciones con la
                # etiqueta, 8 no tenian human_takeover (3 de ellas abiertas:
                # 114, 126 y 133). Para esas ocho la admision pasa, el agente
                # corre, y recien despues la guarda de pre-envio lo frena por
                # la etiqueta. Mirar solo la admision las dejaba afuera.
                #
                # `message_id` se toma del payload ACA, no se hereda: en este
                # camino ninguna rama anterior lo asigna (las que lo hacen,
                # el reset y la planificacion de descuento, terminan en
                # return), y como la funcion lo asigna mas abajo Python lo
                # trata como local. Leerlo sin asignar fue el
                # UnboundLocalError que el 2026-09-23 dejo sin respuesta a
                # los cinco mensajes entrantes de la noche (conv 126, 143,
                # 158 y 63). Es la clave de idempotencia de la reanudacion
                # (`resume:<conversation_id>:<message_id>`).
                resume_message_id = payload.get("id")
                if (
                    not isinstance(resume_message_id, int)
                    or isinstance(resume_message_id, bool)
                    or resume_message_id < 1
                ):
                    raise RuntimeError("chatwoot_resume_trigger_message_id_invalid")
                # La identidad contra la que se verifica la conversacion es la
                # del remitente de ESTE webhook, no ALLOWED_WHATSAPP_JID: en
                # modo scoped ese valor es el numero de prueba y ningun lead
                # real lo tiene (25/09/2026, conv 177).
                resumed = await _resume_paused_conversation(
                    control_client=control_client,
                    supabase=shared_supabase,
                    settings=settings,
                    conversation_id=conversation_id,
                    message_id=resume_message_id,
                    expected_jid=scoped_expected_jid,
                    # Con la admision bloqueada y sin etiqueta, la etiqueta la
                    # saco una persona: la pausa se levanta igual (conv 173,
                    # 25/09/2026).
                    durable_pause_recorded=admission.outcome == "blocked",
                )
                # Re-pedir la admision solo tiene sentido si estaba bloqueada.
                # Cuando ya pasaba, lo que faltaba era sacar la etiqueta, y eso
                # ya ocurrio adentro de _resume_paused_conversation.
                if resumed and admission.outcome == "blocked":
                    try:
                        admission = (
                            await admit_inbound_commercial_case(
                                scope_key=settings.chatwoot_cut_b_scope_key,
                                scope_version=(
                                    settings.chatwoot_cut_b_scope_version
                                ),
                                external_conversation_id=conversation_id,
                                external_user_id=external_user_id,
                            )
                        )
                    except SupabaseError as exc:
                        raise RetryableChatwootWorkError(
                            "chatwoot_cut_b_admission_failed"
                        ) from exc
            logger.info(
                "chatwoot_cut_b_admitted outcome=%s",
                admission.outcome,
            )
            if (
                not settings.chatwoot_cut_b_agent_enabled
                or admission.outcome in {"evidence_conflict", "blocked"}
            ):
                return
        audio_handoff_reason: str | None = None
        if audio_transcriber is not None and needs_audio_transcription(payload):
            try:
                transcript = await audio_transcriber.transcribe_message(payload)
            except AudioTranscriptionError as exc:
                logger.warning(
                    "chatwoot_audio_transcription_failed message=%s reason=%s",
                    payload.get("id"),
                    exc.reason,
                )
                audio_handoff_reason = "audio_transcription_failed"
            else:
                payload = {**payload, "content": transcript}
        context = _shadow_context(payload)
        if context is None and audio_handoff_reason is None:
            # Un entrante sin texto (audio con la transcripcion apagada, imagen,
            # sticker) no llega al agente. Antes salia de aca sin dejar rastro:
            # asi se perdieron cinco audios entre el 22 y el 28/09/2026.
            logger.warning(
                "chatwoot_inbound_without_text_ignored message=%s",
                payload.get("id"),
            )
            return
        if audio_handoff_reason is not None:
            # Fallaron todos los modelos: el audio no se entiende y el agente no
            # puede contestar algo que no leyo. Lo toma una persona.
            if not settings.human_handoff_admission_enabled or admission is None:
                logger.warning(
                    "chatwoot_audio_handoff_unavailable message=%s",
                    payload.get("id"),
                )
                return
            completed_proposal: dict[str, object] | None = {
                "decision": "handoff",
                "reply": "",
                "qualification_status": "needs_human",
                "reason_code": audio_handoff_reason,
            }
            must_scan_canonical_history = False
        else:
            assert context is not None
            if settings.payment_link_enabled:
                context["payment_link_action"] = {
                    "enabled": True,
                    "decision": "send_payment_link",
                    "bridge_injects_exact_url": True,
                    "agent_must_not_include_url": True,
                }
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
            assert context is not None
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
            and _requires_medication_guidance_handoff(
                payload.get("content"),
                subject_re=medication_subject_re,
                action_re=medication_action_re,
            )
        ):
            # El motivo tambien se reescribe: si solo cambiara la decision, la
            # fila y la nota se quedarian con el reason_code que el agente habia
            # elegido para otra cosa (por ejemplo payment_link_requested).
            completed_proposal = {
                **completed_proposal,
                "decision": "handoff",
                "reason_code": "direct_medication_guidance",
            }
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
        async def request_current_inbound_handoff(
            proposal: dict[str, object],
        ) -> None:
            if not settings.human_handoff_admission_enabled:
                raise RuntimeError("chatwoot_inbound_handoff_not_configured")
            assert shared_supabase is not None
            assert admission is not None
            assert settings.handoff_projection_policy_key is not None
            assert settings.handoff_projection_policy_version is not None
            try:
                handoff = await request_handoff_for_inbound_proposal(
                    proposal=proposal,
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

        if settings.human_handoff_admission_enabled and (
            completed_proposal.get("decision") == "handoff"
        ):
            await request_current_inbound_handoff(completed_proposal)
            return
        if completed_proposal.get("decision") == "send_payment_link":
            if not settings.payment_link_enabled:
                await request_current_inbound_handoff({
                    **completed_proposal,
                    "decision": "handoff",
                    "qualification_status": "needs_human",
                    "reason_code": "payment_link_disabled",
                })
                return
            assert shared_supabase is not None
            assert admission is not None
            assert settings.chatwoot_account_id is not None
            assert settings.chatwoot_inbox_id is not None
            assert control_client is not None
            try:
                delivery = await deliver_checkout_issuance_v2(
                    # Con manifiesto, la reserva busca la intencion por las
                    # dos formas del movil. Sin manifiesto, como siempre.
                    supabase=(
                        _PhoneEquivalentCheckoutIssuance(shared_supabase)
                        if whatsapp_inbound_equivalence
                        else shared_supabase
                    ),
                    control_client=control_client,
                    commercial_case_id=admission.commercial_case_id,
                    external_user_id=external_user_id,
                    chatwoot_account_id=settings.chatwoot_account_id,
                    chatwoot_inbox_id=settings.chatwoot_inbox_id,
                    chatwoot_conversation_id=conversation_id,
                    trigger_message_id=message_id,
                    delivery_id=delivery_id,
                    preamble=reply,
                    expected_jid=scoped_expected_jid,
                    # Con plantilla, el link viaja en su boton: primero el
                    # texto del agente y, tras la pausa entre partes, la
                    # plantilla. Si no se puede usar, sale escrito.
                    link_template_name=settings.payment_link_template_name,
                    link_template_language=settings.payment_link_template_language,
                    part_delay_seconds=settings.reply_part_delay_seconds,
                    part_sleep=reply_part_sleep,
                )
            except CheckoutDeliveryError as exc:
                raise RetryableChatwootWorkError(exc.code) from exc
            if delivery.outcome == "blocked":
                reason = delivery.reason or "blocked"
                logger.info("payment_link_handoff reason=%s", reason)
                await request_current_inbound_handoff({
                    **completed_proposal,
                    "decision": "handoff",
                    "qualification_status": "needs_human",
                    "reason_code": f"payment_link_{reason}",
                })
                return
            logger.info(
                "payment_link_delivery outcome=%s issuance_id=%s",
                delivery.outcome,
                delivery.issuance_id,
            )
            return
        # V1 remains as rollback-only code during the V2 rolling transition.
        if False and completed_proposal.get("decision") == "send_payment_link":
            if not settings.payment_link_enabled:
                await request_current_inbound_handoff({
                    **completed_proposal,
                    "decision": "handoff",
                    "qualification_status": "needs_human",
                    "reason_code": "payment_link_disabled",
                })
                return
            assert shared_supabase is not None
            assert admission is not None
            assert settings.chatwoot_account_id is not None
            assert settings.chatwoot_inbox_id is not None
            assert control_client is not None
            now = datetime.now(UTC)
            try:
                candidate = await shared_supabase.get_chatwoot_payment_link_candidate(
                    commercial_case_id=admission.commercial_case_id,
                    external_user_id=external_user_id,
                    chatwoot_account_id=settings.chatwoot_account_id,
                    chatwoot_inbox_id=settings.chatwoot_inbox_id,
                    chatwoot_conversation_id=conversation_id,
                    max_age_seconds=payment_link_config.max_age_seconds,
                    now=now.isoformat(),
                )
            except SupabaseError as exc:
                raise RetryableChatwootWorkError(
                    "payment_link_candidate_lookup_failed"
                ) from exc
            if candidate.outcome != "available":
                logger.info("payment_link_handoff reason=%s", candidate.outcome)
                await request_current_inbound_handoff({
                    **completed_proposal,
                    "decision": "handoff",
                    "qualification_status": "needs_human",
                    "reason_code": f"payment_link_{candidate.outcome}",
                })
                return
            assert candidate.source_reevaluation_id is not None
            assert candidate.source_submission_id is not None
            assert candidate.sequence_origin_event_ulid is not None
            assert candidate.canonical_checkout_url is not None
            assert candidate.submitted_at is not None
            try:
                payment_link = build_payment_link(
                    checkout_url=candidate.canonical_checkout_url,
                    sequence_origin_event_id=candidate.sequence_origin_event_ulid,
                    submitted_at=datetime.fromisoformat(candidate.submitted_at),
                    now=now,
                    config=payment_link_config,
                )
            except (PaymentLinkUnavailable, ValueError):
                logger.info("payment_link_handoff reason=deterministic_builder_rejected")
                await request_current_inbound_handoff({
                    **completed_proposal,
                    "decision": "handoff",
                    "qualification_status": "needs_human",
                    "reason_code": "payment_link_builder_rejected",
                })
                return
            reservation = None

            async def authorize_payment_link_send() -> bool:
                nonlocal reservation
                try:
                    reservation = await shared_supabase.prepare_chatwoot_payment_link_send(
                        commercial_case_id=admission.commercial_case_id,
                        external_user_id=external_user_id,
                        chatwoot_account_id=settings.chatwoot_account_id,
                        chatwoot_inbox_id=settings.chatwoot_inbox_id,
                        chatwoot_conversation_id=conversation_id,
                        trigger_external_message_id=str(message_id),
                        max_age_seconds=payment_link_config.max_age_seconds,
                        source_reevaluation_id=candidate.source_reevaluation_id,
                        source_submission_id=candidate.source_submission_id,
                        checkout_url_original=candidate.canonical_checkout_url,
                        checkout_url_final=payment_link.final_url,
                        tracking_field=payment_link.tracking_field,
                        tracking_value=payment_link.tracking_value,
                        tracking_prefix=payment_link_config.tracking_prefix,
                        now=datetime.now(UTC).isoformat(),
                    )
                except SupabaseError as exc:
                    raise RetryableChatwootWorkError(
                        "payment_link_send_prepare_failed"
                    ) from exc
                if reservation.outcome != "request_started":
                    return False
                if (
                    reservation.checkout_url_final != payment_link.final_url
                    or reservation.tracking_field != payment_link.tracking_field
                    or reservation.tracking_value != payment_link.tracking_value
                ):
                    if reservation.send_command_id is None:
                        raise RetryableChatwootWorkError(
                            "payment_link_send_reservation_mismatch"
                        )
                    try:
                        await shared_supabase.finalize_chatwoot_payment_link_send(
                            send_command_id=reservation.send_command_id,
                            status="delivery_unknown",
                            chatwoot_message_id=None,
                            failure_code="payment_link_reservation_mismatch",
                            now=datetime.now(UTC).isoformat(),
                        )
                    except SupabaseError as finalize_exc:
                        raise RetryableChatwootWorkError(
                            "payment_link_delivery_unknown_finalize_failed"
                        ) from finalize_exc
                    raise RetryableChatwootWorkError(
                        "payment_link_send_reservation_mismatch"
                    )
                return True

            try:
                payment_reply = render_payment_link_reply(
                    preamble=reply,
                    final_url=payment_link.final_url,
                )
            except PaymentLinkUnavailable:
                logger.info("payment_link_handoff reason=agent_reply_rejected")
                await request_current_inbound_handoff({
                    **completed_proposal,
                    "decision": "handoff",
                    "qualification_status": "needs_human",
                    "reason_code": "payment_link_reply_rejected",
                })
                return
            send_args = {
                "conversation_id": conversation_id,
                "trigger_message_id": message_id,
                "delivery_id": delivery_id,
                "content": payment_reply,
                "pre_send_authorizer": authorize_payment_link_send,
                "expected_inbox_id": settings.chatwoot_inbox_id,
                "agent_decision": "send_payment_link",
            }
            payment_reason_marker = _agent_marker(
                completed_proposal.get("reason_code")
            )
            if payment_reason_marker is not None:
                send_args["agent_reason_code"] = payment_reason_marker
            if scoped_expected_jid is not None:
                send_args["expected_jid"] = scoped_expected_jid
            try:
                payment_result = await control_client.send_agent_bot_reply(**send_args)
            except ChatwootReplyDeliveryUnknownError as exc:
                if reservation is None or reservation.send_command_id is None:
                    raise RetryableChatwootWorkError(
                        "payment_link_delivery_unknown_before_reservation"
                    ) from exc
                try:
                    await shared_supabase.finalize_chatwoot_payment_link_send(
                        send_command_id=reservation.send_command_id,
                        status="delivery_unknown",
                        chatwoot_message_id=None,
                        failure_code="chatwoot_delivery_unknown",
                        now=datetime.now(UTC).isoformat(),
                    )
                except SupabaseError as finalize_exc:
                    raise RetryableChatwootWorkError(
                        "payment_link_delivery_unknown_finalize_failed"
                    ) from finalize_exc
                raise RetryableChatwootWorkError(
                    "payment_link_delivery_unknown"
                ) from exc
            except (ChatwootProtocolError, httpx.HTTPError) as exc:
                if reservation is not None and reservation.send_command_id is not None:
                    try:
                        await shared_supabase.finalize_chatwoot_payment_link_send(
                            send_command_id=reservation.send_command_id,
                            status="delivery_unknown",
                            chatwoot_message_id=None,
                            failure_code="chatwoot_send_unconfirmed",
                            now=datetime.now(UTC).isoformat(),
                        )
                    except SupabaseError as finalize_exc:
                        raise RetryableChatwootWorkError(
                            "payment_link_delivery_unknown_finalize_failed"
                        ) from finalize_exc
                raise RetryableChatwootWorkError(
                    "payment_link_send_unconfirmed"
                ) from exc
            payment_status = payment_result.get("status")
            payment_message_id = payment_result.get("message_id")
            payment_result_confirmed = (
                payment_status in {"sent", "duplicate"}
                and isinstance(payment_message_id, int)
                and not isinstance(payment_message_id, bool)
                and payment_message_id > 0
            )
            if (
                reservation is None
                and payment_status == "duplicate"
                and payment_result_confirmed
            ):
                await authorize_payment_link_send()
            if reservation is None:
                logger.info(
                    "payment_link_send_blocked_before_final_authorization status=%s",
                    payment_status,
                )
                return
            if reservation.outcome == "already_accepted":
                logger.info("payment_link_send_replayed outcome=%s", reservation.outcome)
                return
            if (
                reservation.outcome == "delivery_unknown"
                and payment_status != "duplicate"
            ):
                logger.info("payment_link_send_replayed outcome=%s", reservation.outcome)
                return
            if reservation.outcome not in {"request_started", "delivery_unknown"}:
                logger.info("payment_link_handoff reason=%s", reservation.outcome)
                await request_current_inbound_handoff({
                    **completed_proposal,
                    "decision": "handoff",
                    "qualification_status": "needs_human",
                    "reason_code": f"payment_link_{reservation.outcome}",
                })
                return
            assert reservation.send_command_id is not None
            if not payment_result_confirmed:
                raise RuntimeError("invalid_payment_link_reply_result")
            try:
                await shared_supabase.finalize_chatwoot_payment_link_send(
                    send_command_id=reservation.send_command_id,
                    status="accepted_by_chatwoot",
                    chatwoot_message_id=payment_message_id,
                    failure_code=None,
                    now=datetime.now(UTC).isoformat(),
                )
            except SupabaseError as exc:
                raise RetryableChatwootWorkError(
                    "payment_link_acceptance_finalize_failed"
                ) from exc
            logger.info(
                "payment_link_send_finalized command_id=%s status=%s",
                reservation.send_command_id,
                payment_status,
            )
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

        reply_decision_marker = _agent_marker(completed_proposal.get("decision"))
        reply_reason_marker = _agent_marker(completed_proposal.get("reason_code"))
        try:
            for offset, part in enumerate(parts):
                if offset > 0:
                    await reply_part_sleep(settings.reply_part_delay_seconds)
                if len(parts) == 1:
                    reply_result = await send_scoped_agent_bot_reply(
                        conversation_id=conversation_id,
                        trigger_message_id=message_id,
                        content=part,
                        agent_decision=reply_decision_marker,
                        agent_reason_code=reply_reason_marker,
                    )
                else:
                    reply_result = await send_scoped_agent_bot_reply(
                        conversation_id=conversation_id,
                        trigger_message_id=message_id,
                        content=part,
                        part_index=offset + 1,
                        part_count=len(parts),
                        prior_parts=parts[:offset],
                        agent_decision=reply_decision_marker,
                        agent_reason_code=reply_reason_marker,
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
        if settings.chatwoot_stalled_monitor_enabled:
            if control_client is None or settings.chatwoot_inbox_id is None:
                raise ValueError("stalled monitor requires Chatwoot control client")
            monitor_inbox_id = settings.chatwoot_inbox_id

            async def scan_stalled() -> list[object]:
                return list(
                    await control_client.list_stalled_conversations(
                        expected_inbox_id=monitor_inbox_id,
                        stale_after_seconds=settings.chatwoot_stalled_after_seconds,
                        max_age_seconds=settings.chatwoot_stalled_max_age_seconds,
                        max_pages=settings.chatwoot_stalled_max_pages,
                        allow_any_scoped_sender=(
                            settings.chatwoot_scoped_inbound_senders_enabled
                        ),
                    )
                )

            chatwoot_stalled_monitor = ChatwootStalledConversationMonitor(
                inbox=chatwoot_inbox,
                scanner=scan_stalled,
                scan_interval_seconds=(
                    settings.chatwoot_stalled_monitor_interval_seconds
                ),
                recovery_cooldown_seconds=(
                    settings.chatwoot_stalled_recovery_cooldown_seconds
                ),
                recovery_max_admissions=(
                    settings.chatwoot_stalled_max_recovery_admissions
                ),
            )
            app.state.chatwoot_stalled_monitor = chatwoot_stalled_monitor
        if settings.conversation_reactivation_enabled:
            if (
                not isinstance(control_client, ChatwootClient)
                or settings.chatwoot_inbox_id is None
                or shared_supabase is None
                or settings.conversation_reactivation_template_name is None
            ):
                raise ValueError(
                    "conversation reactivation requires the Chatwoot control "
                    "client, a canonical inbox and Supabase"
                )
            conversation_reactivation_sweeper = ConversationReactivationSweeper(
                chatwoot=control_client,
                supabase=shared_supabase,
                inbox_id=settings.chatwoot_inbox_id,
                template_name=(
                    settings.conversation_reactivation_template_name
                ),
                expected_template_language=(
                    settings.conversation_reactivation_template_language
                ),
                scan_interval_seconds=(
                    settings.conversation_reactivation_interval_seconds
                ),
                min_inbound_age_seconds=(
                    settings.conversation_reactivation_min_age_seconds
                ),
                max_inbound_age_seconds=(
                    settings.conversation_reactivation_max_age_seconds
                ),
                quiet_seconds_threshold=(
                    settings.conversation_resume_quiet_seconds
                ),
                max_reactivations=settings.conversation_reactivation_max,
                max_sends_per_scan=(
                    settings.conversation_reactivation_max_sends_per_scan
                ),
                max_pages=settings.conversation_reactivation_max_pages,
                # Mismo criterio que el monitor de conversaciones estancadas
                # (`allow_any_scoped_sender`): con los remitentes acotados por
                # scope, el limite no es un JID unico sino las barreras de
                # opt-out, pausa y handoff. Restringir igual al JID deja afuera
                # a todo el inbox, porque ALLOWED_WHATSAPP_JID es un numero de
                # prueba. Medido el 2026-09-23 22:40 UTC: las 25 conversaciones
                # abiertas del inbox 9 se saltearon con `target_not_allowed`.
                allowed_phone=(
                    None
                    if settings.chatwoot_scoped_inbound_senders_enabled
                    else allowed_phone_from_jid(settings.allowed_jid)
                ),
            )
            app.state.conversation_reactivation_sweeper = (
                conversation_reactivation_sweeper
            )
        if settings.conversation_followup_enabled:
            if (
                not isinstance(control_client, ChatwootClient)
                or settings.chatwoot_account_id is None
                or settings.chatwoot_inbox_id is None
                or shared_supabase is None
                or settings.conversation_followup_template_name is None
                or settings.conversation_followup_coupon_code is None
                or settings.conversation_followup_product_name is None
            ):
                raise ValueError(
                    "conversation followup requires the Chatwoot control "
                    "client, canonical ids and Supabase"
                )
            conversation_followup_sweeper = ConversationFollowupSweeper(
                chatwoot=control_client,
                supabase=shared_supabase,
                account_id=settings.chatwoot_account_id,
                inbox_id=settings.chatwoot_inbox_id,
                template_name=settings.conversation_followup_template_name,
                coupon_code=settings.conversation_followup_coupon_code,
                product_name=settings.conversation_followup_product_name,
                expected_template_language=(
                    settings.conversation_followup_template_language
                ),
                scan_interval_seconds=(
                    settings.conversation_followup_interval_seconds
                ),
                min_inbound_age_seconds=(
                    settings.conversation_followup_min_age_seconds
                ),
                max_inbound_age_seconds=(
                    settings.conversation_followup_max_age_seconds
                ),
                max_sends_per_scan=(
                    settings.conversation_followup_max_sends_per_scan
                ),
                max_pages=settings.conversation_followup_max_pages,
                # Mismo criterio que la reactivacion: con los remitentes
                # acotados por scope, ALLOWED_WHATSAPP_JID es un numero de
                # prueba y restringir a el dejaria afuera a todo el inbox.
                allowed_phone=(
                    None
                    if settings.chatwoot_scoped_inbound_senders_enabled
                    else allowed_phone_from_jid(settings.allowed_jid)
                ),
            )
            app.state.conversation_followup_sweeper = (
                conversation_followup_sweeper
            )

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
            case_id: str | None = None,
            authorization: str | None = Header(default=None, alias="Authorization"),
        ) -> dict[str, object]:
            require_operator_token(authorization)
            if limit < 1 or limit > 50:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="limit_out_of_range",
                )
            normalized_case_id: str | None = None
            if case_id is not None:
                try:
                    normalized_case_id = str(uuid.UUID(case_id))
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="invalid_case_id",
                    ) from exc
            try:
                raw_rows = (
                    await shared_supabase.list_unresolved_purchase_intent_correlations(
                        tenant_ref=operator_tenant,
                        funnel_ref=operator_funnel,
                        limit=limit,
                        webhook_event_id=normalized_case_id,
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
        async def get_masked_unresolved_correlation(
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
                raw_rows = await shared_supabase.list_unresolved_purchase_intent_correlations(
                    tenant_ref=operator_tenant,
                    funnel_ref=operator_funnel,
                    limit=1,
                    webhook_event_id=normalized_case_id,
                )
                if len(raw_rows) != 1:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="unresolved_correlation_not_found",
                    )
                case = build_unresolved_correlation(
                    raw_rows[0], include_candidates=False
                )
            except HTTPException:
                raise
            except (SupabaseError, InvalidCorrelationEvidence) as exc:
                logger.warning(
                    "operator_correlation_masked_get_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="operator_correlation_read_unavailable",
                ) from exc
            return {"case": case}

        @app.get(
            "/internal/operator/correlations/unresolved/{case_id}/private-review"
        )
        async def get_private_unresolved_correlation(
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
                    webhook_event_id=normalized_case_id,
                )
                if raw is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="unresolved_correlation_not_found",
                    )
                raw_identity = raw.get("identity")
                has_private_identity = isinstance(raw_identity, dict) and (
                    "normalized_email" in raw_identity
                    or "normalized_phone" in raw_identity
                )
                case = build_unresolved_correlation(
                    raw,
                    include_candidates=True,
                    include_private_identity=has_private_identity,
                )
            except HTTPException:
                raise
            except (SupabaseError, InvalidCorrelationEvidence) as exc:
                logger.warning(
                    "operator_correlation_private_get_failed error_type=%s",
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
        operator_actor_prefix = settings.operator_correlation_actor_prefix
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
                effective_actor = resolve_actor_ref(
                    prepared["actor_ref"],
                    fallback_actor_ref=operator_actor,
                    actor_prefix=operator_actor_prefix,
                )
                raw = await shared_supabase.prepare_operator_correlation_resolution(
                    tenant_ref=operator_tenant,
                    funnel_ref=operator_funnel,
                    actor_ref=effective_actor,
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
                effective_actor = resolve_actor_ref(
                    confirmation["actor_ref"],
                    fallback_actor_ref=operator_actor,
                    actor_prefix=operator_actor_prefix,
                )
                raw = await shared_supabase.confirm_operator_correlation_resolution(
                    tenant_ref=operator_tenant,
                    funnel_ref=operator_funnel,
                    actor_ref=effective_actor,
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
        stalled_monitor_readiness = {
            "chatwoot_stalled_monitor": "disabled",
        }
        if chatwoot_stalled_monitor is not None:
            if not chatwoot_stalled_monitor.ready:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "chatwoot_stalled_monitor_"
                        f"{chatwoot_stalled_monitor.last_scan_state}"
                    ),
                )
            stalled_monitor_readiness = {
                **stalled_monitor_readiness,
                "chatwoot_stalled_monitor": "healthy",
            }
        if conversation_reactivation_sweeper is not None:
            # Un barredor caido no responde 503: no atiende a nadie en vivo, y
            # tumbar el bridge entero por eso dejaria de contestarle a los leads
            # que si estan escribiendo. Se publica el estado y se ve. La clave
            # solo aparece cuando el barredor existe, para no cambiarle el
            # payload de readiness a los despliegues que no lo usan.
            stalled_monitor_readiness = {
                **stalled_monitor_readiness,
                "conversation_reactivation": (
                    conversation_reactivation_sweeper.last_scan_state
                ),
                # El estado solo dice si el barrido fallo. Saltear a las 25
                # conversaciones del inbox no es una falla, asi que sin este
                # resumen un barredor mal configurado y uno sin trabajo
                # publican lo mismo.
                "conversation_reactivation_last_scan": (
                    conversation_reactivation_sweeper.last_scan_summary
                ),
            }
        if conversation_followup_sweeper is not None:
            # Igual que la reactivacion: un barredor caido no tumba el bridge,
            # se publica. El resumen distingue 'sin candidatos' de 'salteo a
            # todos por un motivo'.
            stalled_monitor_readiness = {
                **stalled_monitor_readiness,
                "conversation_followup": (
                    conversation_followup_sweeper.last_scan_state
                ),
                "conversation_followup_last_scan": (
                    conversation_followup_sweeper.last_scan_summary
                ),
            }
        if (
            correlation_preresolution_worker is not None
            and not correlation_preresolution_worker.healthy
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="correlation_preresolution_unhealthy",
            )
        if slack_projection_worker is not None and slack_projection_worker.halted:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="slack_projection_halted",
            )
        if slack_projection_worker is not None and not slack_projection_worker.healthy:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="slack_projection_unhealthy",
            )
        commercial_ally_readiness: dict[str, str] = {}
        if portable_runtime:
            if shared_supabase is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="commercial_ally_binding_unavailable",
                )
            try:
                await shared_supabase.resolve_commercial_ally_runtime_binding(
                    settings.commercial_ally_config
                )
            except Exception as exc:
                logger.warning(
                    "commercial_ally_binding_check_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="commercial_ally_binding_unavailable",
                ) from exc
            commercial_ally_readiness = {
                "commercial_ally_binding": "active",
            }
        commercial_ally_readiness = {
            **commercial_ally_readiness,
            **instance_readiness,
        }
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
        # El primer contacto del formulario tiene su propio scope del piloto:
        # se publica su estado (inactive, armed, paused, closed). La clave solo
        # aparece con el flag, para no cambiarle el payload a quien no lo usa.
        first_contact_readiness: dict[str, str] = {}
        if precheckout_pilot_boundary is not None:
            if shared_supabase is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="portable_precheckout_readiness_unavailable",
                )
            try:
                first_contact_status = await (
                    shared_supabase.get_portable_precheckout_pilot_runtime_status(
                        pilot_boundary=precheckout_pilot_boundary
                    )
                )
            except Exception as exc:
                logger.warning(
                    "portable_precheckout_readiness_check_failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="portable_precheckout_readiness_unavailable",
                ) from exc
            if (
                not first_contact_status.configured
                or first_contact_status.runtime_state is None
            ):
                # Scope sin publicar, de otra fuente, manual_cohort o con otra
                # version activa: el flujo no puede planificar ni mandar.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "portable_precheckout_"
                        f"{first_contact_status.reason_code}"
                    ),
                )
            first_contact_readiness = {
                "portable_precheckout_first_contact": (
                    first_contact_status.runtime_state
                ),
            }
        if pilot_boundary is None:
            return {
                "status": "ready",
                "pilot_boundary": "disabled",
                "automation_state": "default_off",
                "reason_code": "pilot_boundary_disabled",
                **commercial_ally_readiness,
                **precheckout_readiness,
                **first_contact_readiness,
                **handoff_readiness,
                **stalled_monitor_readiness,
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
        if ghl_adapter_audience_gate:
            # La misma lectura del arranque (que ya corta antes de los
            # workers): aca el motivo queda visible para quien consulta /ready.
            audience_block = await _ghl_adapter_audience_block(
                shared_supabase, pilot_boundary
            )
            if audience_block is not None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=audience_block,
                )
        return {
            "status": "ready",
            "pilot_boundary": "configured",
            "automation_state": pilot_status.runtime_state,
            "reason_code": pilot_status.reason_code,
            **commercial_ally_readiness,
            **precheckout_readiness,
            **first_contact_readiness,
            **handoff_readiness,
            **stalled_monitor_readiness,
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
        if not decision.accepted and _is_conversation_label_change(
            payload, inbox_id=settings.chatwoot_inbox_id
        ):
            # Sin trabajo durable: solo la captura, para tener el payload real
            # antes de sincronizar la etiqueta con la capa durable.
            if not _capture_payload(
                capture_dir=settings.capture_dir,
                delivery_id=x_chatwoot_delivery,
                payload=payload,
            ):
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "duplicate",
                    "delivery_id": x_chatwoot_delivery,
                }
            logger.warning(
                "chatwoot_conversation_label_change_captured delivery=%s",
                x_chatwoot_delivery,
            )
            return {
                "status": "captured",
                "reason": "conversation_label_change",
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
                or (
                    audio_transcriber is not None
                    and needs_audio_transcription(payload)
                )
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

    @app.post(
        "/webhooks/johanna-funnel-events",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def receive_johanna_funnel_event(
        request: Request,
        response: Response,
        content_type: str = Header(default=""),
        x_lancemos_signature: str = Header(default=""),
    ) -> dict[str, object]:
        if settings.lead_precheckout_secret is None:
            raise HTTPException(status_code=503, detail="johanna_funnel_not_enabled")
        secret = settings.lead_precheckout_secret
        if secret is None or shared_supabase is None:
            raise HTTPException(status_code=503, detail="johanna_funnel_not_configured")
        normalized_content_type = ";".join(
            part.strip().lower() for part in content_type.split(";")
        )
        if normalized_content_type != "application/json;charset=utf-8":
            raise HTTPException(status_code=400, detail="invalid_johanna_funnel_transport")

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > JOHANNA_FUNNEL_EVENT_BODY_LIMIT_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="johanna_funnel_event_too_large",
                )
            body.extend(chunk)
        raw_body = bytes(body)
        expected_signature = "sha256=" + hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(x_lancemos_signature, expected_signature):
            raise HTTPException(
                status_code=401, detail="invalid_johanna_funnel_signature"
            )
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc

        top_fields = {
            "version", "event_id", "event_type", "occurred_at",
            "anonymous_session_id", "landing_ref", "offer_ref", "utm",
        }
        utm_fields = {"source", "medium", "campaign", "content", "term"}
        pairs = {
            ("ads-a", "bxjge6zq"), ("ads-b", "mgbgpp19"),
            ("ads-c", "s1qfxm7m"), ("org-a", "jtt6fcsm"),
            ("org-b", "ecyu87q0"), ("org-c", "ulhzpw9a"),
        }
        ulid_pattern = r"[0-9A-HJKMNP-TV-Z]{26}"
        valid = isinstance(payload, dict) and set(payload) == top_fields
        utm = payload.get("utm") if isinstance(payload, dict) else None
        valid = valid and isinstance(utm, dict) and set(utm) == utm_fields
        if valid:
            assert isinstance(payload, dict) and isinstance(utm, dict)
            valid = (
                payload.get("version") == "1.0.0"
                and payload.get("event_type") in {
                    "page_view", "preform_opened", "preform_submitted",
                    "checkout_redirected",
                }
                and isinstance(payload.get("event_id"), str)
                and re.fullmatch(ulid_pattern, payload["event_id"]) is not None
                and isinstance(payload.get("anonymous_session_id"), str)
                and re.fullmatch(ulid_pattern, payload["anonymous_session_id"])
                is not None
                and (payload.get("landing_ref"), payload.get("offer_ref")) in pairs
                and all(
                    value is None
                    or (isinstance(value, str) and len(value) <= 128)
                    for value in utm.values()
                )
            )
            try:
                occurred_at = datetime.fromisoformat(
                    str(payload.get("occurred_at", "")).replace("Z", "+00:00")
                )
                server_now = datetime.now(UTC)
                valid = (
                    valid
                    and occurred_at.tzinfo is not None
                    and server_now - JOHANNA_FUNNEL_EVENT_MAX_AGE
                    <= occurred_at.astimezone(UTC)
                    <= server_now + JOHANNA_FUNNEL_EVENT_FUTURE_TOLERANCE
                )
            except ValueError:
                valid = False
        if not valid:
            raise HTTPException(status_code=400, detail="invalid_johanna_funnel_event")

        assert isinstance(payload, dict) and isinstance(utm, dict)
        try:
            admission = await shared_supabase.admit_johanna_funnel_event(
                version=payload["version"],
                event_id=payload["event_id"],
                event_type=payload["event_type"],
                occurred_at=payload["occurred_at"],
                anonymous_session_id=payload["anonymous_session_id"],
                landing_ref=payload["landing_ref"],
                offer_ref=payload["offer_ref"],
                utm_source=utm["source"],
                utm_medium=utm["medium"],
                utm_campaign=utm["campaign"],
                utm_content=utm["content"],
                utm_term=utm["term"],
            )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503, detail="johanna_funnel_persist_unavailable"
            ) from exc
        if admission.outcome == "duplicate":
            response.status_code = status.HTTP_200_OK
            return {"status": "duplicate", "event_id": admission.event_id}
        if admission.outcome == "semantic_conflict":
            response.status_code = status.HTTP_409_CONFLICT
            return {"status": "conflict", "event_id": admission.event_id}
        return {"status": "received", "event_id": admission.event_id}

    def _lead_admission_response(
        submission: LeadPrecheckoutSubmission,
        admission: PrecheckoutAdmissionResult,
        background_tasks: BackgroundTasks,
    ) -> dict[str, object]:
        # La respuesta de una admision de lead.precheckout, la misma para
        # /webhooks/lead y para el adaptador de GHL.
        response_status = {
            "inserted": "received",
            "duplicate": "duplicate",
            "semantic_conflict": "conflict",
        }[admission.outcome]
        if (
            admission.outcome == "inserted"
            and lead_first_name_client is not None
            and shared_supabase is not None
        ):
            # Despues de responder: el formulario ya quedo admitido y el modelo
            # nunca demora ni rompe esa respuesta.
            background_tasks.add_task(
                infer_and_record_first_name,
                full_name=submission.buyer_name,
                client=lead_first_name_client,
                store=shared_supabase,
            )
        return {
            "status": response_status,
            "delivery_id": submission.external_submission_id,
            "purchase_intent_id": admission.purchase_intent_id,
            "activation_authorized": False,
            "contact_authorized": False,
        }

    async def _admit_portable_lead(
        submission: LeadPrecheckoutSubmission,
        raw_payload: dict[str, object],
    ) -> PrecheckoutAdmissionResult:
        # La admision portable de lead.precheckout, la misma para /webhooks/lead
        # con manifiesto y para el adaptador de GHL. Apagado el primer contacto,
        # es la RPC de siempre. Prendido, la base admite el envio y planifica
        # el primer contacto en la misma transaccion: la admision no se pierde
        # si el plan no procede, y la respuesta HTTP es la misma en los dos
        # casos.
        assert shared_supabase is not None
        if not settings.portable_precheckout_first_contact_enabled:
            return await shared_supabase.admit_portable_observed_lead_precheckout(
                config=settings.commercial_ally_config,
                external_submission_id=submission.external_submission_id,
                raw_payload=raw_payload,
                canonical_payload=submission.as_canonical_payload(),
            )
        # create_app ya no arranca sin el scope con el flag prendido.
        assert settings.pilot_precheckout_scope_key is not None
        assert settings.pilot_precheckout_scope_version is not None
        admission = await shared_supabase.admit_and_plan_portable_lead_precheckout(
            config=settings.commercial_ally_config,
            external_submission_id=submission.external_submission_id,
            raw_payload=raw_payload,
            canonical_payload=submission.as_canonical_payload(),
            scope_key=settings.pilot_precheckout_scope_key,
            scope_version=settings.pilot_precheckout_scope_version,
        )
        # Solo ids y codigos: nunca el nombre, el email ni el telefono. Un plan
        # que fallo sale como warning (bajo uvicorn solo los warnings llegan a
        # la salida del contenedor); el motivo de cada envio queda ademas en
        # portable_precheckout_first_contact_plans.
        logger.log(
            logging.WARNING
            if admission.plan_outcome == "plan_failed"
            else logging.INFO,
            "portable_precheckout_first_contact admission=%s plan=%s reason=%s "
            "submission_id=%s",
            admission.outcome,
            admission.plan_outcome or "-",
            admission.plan_reason or "-",
            admission.submission_id,
        )
        return admission

    @app.post("/webhooks/lead", status_code=status.HTTP_200_OK)
    async def receive_lead_precheckout_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
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
        submission = parse_lead_precheckout(
            payload,
            config=settings.commercial_ally_config,
        )
        if submission is None:
            raise HTTPException(status_code=400, detail="invalid_lead_precheckout_payload")
        if (
            x_lancemos_event != "lead.precheckout"
            or x_lancemos_event != payload.get("event")
            or x_lancemos_delivery != submission.external_submission_id
        ):
            raise HTTPException(status_code=400, detail="lead_header_payload_mismatch")
        if (
            explicit_manifest_runtime
            or settings.commercial_ally_config is not JOHANNA_COMMERCIAL_ALLY
        ):
            # Runtime con binding propio: se admite la terna (sitio, landing,
            # oferta) de cualquier landing declarada en el binding. La por
            # defecto es la de LEAD_PRECHECKOUT_* (create_app exige que
            # coincidan); las demas salen de additional_offer_landings.
            offer_landing = settings.commercial_ally_config.offer_landing(
                submission.site, submission.landing_id
            )
            outside_scope = (
                offer_landing is None
                or offer_landing.offer_code != submission.offer_code
            )
        else:
            outside_scope = submission.site != settings.lead_precheckout_site
        if outside_scope:
            raise HTTPException(status_code=403, detail="lead_precheckout_outside_scope")
        age_seconds = (datetime.now(UTC) - submission.submitted_at).total_seconds()
        if age_seconds < -60 or age_seconds > settings.lead_precheckout_max_age_seconds:
            raise HTTPException(status_code=401, detail="stale_lead_precheckout")
        if shared_supabase is None:
            raise HTTPException(status_code=503, detail="supabase_not_configured")
        assert isinstance(payload, dict)
        try:
            admission = (
                await _admit_portable_lead(submission, payload)
                if explicit_manifest_runtime
                else await shared_supabase.admit_observed_lead_precheckout(
                    external_submission_id=submission.external_submission_id,
                    raw_payload=payload,
                    canonical_payload=submission.as_canonical_payload(),
                )
            )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503, detail="lead_precheckout_persist_unavailable"
            ) from exc
        return _lead_admission_response(submission, admission, background_tasks)

    async def _read_ghl_body(request: Request, content_type: str) -> dict[str, object]:
        # Solo el tipo de medio: GHL manda application/json sin charset, y el
        # User-Agent (axios) es un detalle suyo que no se valida.
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(status_code=400, detail="invalid_ghl_transport")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > PRECHECKOUT_WEBHOOK_BODY_LIMIT_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="ghl_adapter_body_too_large",
                )
            raw.extend(chunk)
        try:
            return parse_ghl_body(bytes(raw))
        except GhlAdapterRejection as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=f"ghl_{exc.reason}"
            ) from None

    async def _admit_ghl_form_submission(
        request: Request,
        background_tasks: BackgroundTasks,
        content_type: str,
        header_token: str,
        trace: _GhlAdapterTrace,
    ) -> dict[str, object]:
        manifest = settings.instance_manifest
        token = settings.ghl_precheckout_adapter_token
        # create_app ya no arranca sin esto con el flag prendido: se repite
        # como defensa.
        if not settings.ghl_precheckout_adapter_enabled or manifest is None or not token:
            raise HTTPException(
                status_code=503, detail="ghl_precheckout_adapter_not_enabled"
            )
        expected_token = token.encode("utf-8")

        def token_matches(candidate: str) -> bool:
            return hmac.compare_digest(
                candidate.encode("utf-8", "surrogatepass"), expected_token
            )

        # Un header distinto corta antes de leer el cuerpo.
        if header_token and not token_matches(header_token):
            raise HTTPException(status_code=401, detail="invalid_adapter_token")
        try:
            body = await _read_ghl_body(request, content_type)
        except HTTPException as exc:
            if header_token:
                raise
            # Sin header el pedido todavia no esta autenticado (el token de la
            # accion Webhook viaja en el cuerpo): quien no lo tiene recibe solo
            # 401, sin saber por que no se leyo el cuerpo. La linea de log
            # conserva el motivo real.
            trace.unauthenticated_reason = str(exc.detail)
            raise HTTPException(
                status_code=401, detail="invalid_adapter_token"
            ) from None

        # La accion Webhook estandar de GHL manda el token en Custom Data. Si
        # viene en los dos lugares, los dos tienen que coincidir.
        body_token = ghl_body_token(body)
        if body_token is None and not header_token:
            raise HTTPException(status_code=401, detail="invalid_adapter_token")
        if body_token is not None and not token_matches(body_token):
            raise HTTPException(status_code=401, detail="invalid_adapter_token")

        trace.note_form(body)
        try:
            translation = translate_ghl_form_submission(
                body,
                config=settings.commercial_ally_config,
                allowed_forms=frozenset(manifest.ghl_form_ids),
                now=datetime.now(UTC),
            )
        except GhlAdapterRejection as exc:
            if exc.phone_region is not None:
                trace.phone_region = exc.phone_region
            raise HTTPException(
                status_code=exc.status_code, detail=f"ghl_{exc.reason}"
            ) from None
        trace.form = translation.form_id
        trace.landing = f"{translation.site}/{translation.landing_id}"
        trace.offer = translation.offer_code
        trace.phone_region = translation.phone_region
        trace.has_utm = str(translation.has_utm).lower()
        trace.has_fbclid = str(translation.has_fbclid).lower()

        submission = parse_lead_precheckout(
            translation.event, config=settings.commercial_ally_config
        )
        if submission is None:
            # Un error del traductor: reintentar no lo arregla, asi que no es
            # 5xx. La linea del pedido sale como warning.
            raise HTTPException(status_code=422, detail="ghl_translation_rejected")
        trace.delivery_id = submission.external_submission_id
        if not submission.phone_valid:
            # La admision portable 1.1.0 lo rechazaria con 22023, que llega como
            # 503 y GHL reintentaria para siempre.
            raise HTTPException(status_code=422, detail="ghl_phone_unusable")
        if shared_supabase is None:
            raise HTTPException(status_code=503, detail="supabase_not_configured")
        try:
            # Siempre la admision portable, con el evento traducido: el cuerpo
            # de GHL no se guarda.
            admission = await _admit_portable_lead(submission, translation.event)
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503, detail="ghl_precheckout_persist_unavailable"
            ) from exc
        return _lead_admission_response(submission, admission, background_tasks)

    @app.post(GHL_PRECHECKOUT_ADAPTER_PATH, status_code=status.HTTP_200_OK)
    async def receive_ghl_precheckout_adapter_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        content_type: str = Header(default=""),
        x_setter_adapter_token: str = Header(default=""),
    ) -> dict[str, object]:
        # docs/contracts/ghl-precheckout-adapter-v1.md. Una linea de log por
        # pedido, con o sin rechazo.
        trace = _GhlAdapterTrace()
        try:
            result = await _admit_ghl_form_submission(
                request, background_tasks, content_type, x_setter_adapter_token, trace
            )
        except HTTPException as exc:
            trace.emit(
                outcome="rejected", status_code=exc.status_code, reason=str(exc.detail)
            )
            raise
        except Exception as exc:
            trace.emit(outcome="error", status_code=500, reason=type(exc).__name__)
            raise
        trace.emit(outcome=str(result["status"]), status_code=200, reason="-")
        return result

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

    async def _greeting_kwargs(full_name: object) -> dict[str, str]:
        """The ``greeting_name`` for a first template, only with the flag on.

        With the flag off it returns nothing, so the sender keeps sending the
        full name exactly as before.
        """
        if (
            not settings.lead_first_name_greeting_enabled
            or not isinstance(full_name, str)
            or not full_name.strip()
        ):
            return {}
        greeting = await resolve_greeting_name(full_name, store=shared_supabase)
        logger.info("first_touch_greeting source=%s", greeting.source)
        return {"greeting_name": greeting.name}

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
            **await _greeting_kwargs(started.buyer_name),
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
        greeting_kwargs = await _greeting_kwargs(buyer_name)
        greeting_name = greeting_kwargs.get("greeting_name", buyer_name)
        content = (
            f"¡Hola, {greeting_name}! Te habla el equipo de Johanna. "
            "Vimos que completaste el formulario de Libre de Ansiedad. "
            "¿Te parece si avanzamos por acá?"
        )
        result = await first_touch_sender.send_first_touch_to_conversation(
            conversation_id=started.chatwoot_conversation_id,
            phone=started.target_phone,
            buyer_name=buyer_name,
            content=content,
            delivery_id=started.command_id,
            **greeting_kwargs,
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
            **await _greeting_kwargs(started.buyer_name),
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

        if (
            settings.portable_hotmart_purchase_stop_enabled
            or settings.portable_hotmart_recovery_enabled
            or settings.portable_hotmart_payment_failure_enabled
        ) and (
            decision.event_type != EVENT_PURCHASE_APPROVED
            and not (
                settings.portable_hotmart_recovery_enabled
                and decision.event_type == EVENT_CART_ABANDONMENT
            )
            and not (
                settings.portable_hotmart_payment_failure_enabled
                and decision.event_type == EVENT_PURCHASE_CANCELED
            )
        ):
            response.status_code = status.HTTP_200_OK
            return {
                "status": "ignored",
                "reason": "portable_purchase_stop_event_ignored",
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
                if not (
                    settings.johanna_payment_failure_hotmart_enabled
                    or settings.portable_hotmart_payment_failure_enabled
                ):
                    response.status_code = status.HTTP_200_OK
                    return {
                        "status": "ignored",
                        "reason": "payment_failure_disabled",
                    }
                parsed_failure = parse_hotmart_payment_failure_payload(
                    payload,
                    config=settings.commercial_ally_config,
                )
                if parsed_failure is None:
                    # Igual que el carrito y la compra: un evento que este runtime no
                    # procesa (otro producto, una oferta fuera del binding) no entra
                    # reenviandolo. Un 4xx lo cuenta Hotmart como falla del webhook y,
                    # sumadas, lo desactiva; el descarte explicito deja el motivo.
                    response.status_code = status.HTTP_200_OK
                    return {
                        "status": "ignored",
                        "reason": "invalid_payment_failure_payload",
                    }
                if settings.portable_hotmart_payment_failure_enabled:
                    portable_failure_admission = (
                        await shared_supabase.admit_portable_hotmart_payment_failure(
                            config=settings.commercial_ally_config,
                            external_event_id=event_id,
                            payload=payload,
                            normalized_email=parsed_failure.buyer_email,
                            normalized_phone=parsed_failure.buyer_phone,
                        )
                    )
                    if portable_failure_admission.outcome == "semantic_conflict":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="payment_failure_semantic_conflict",
                        )
                    if portable_failure_admission.outcome == "duplicate":
                        response.status_code = status.HTTP_200_OK
                        return {"status": "duplicate", "event_id": event_id}
                    return {"status": "received", "event_id": event_id}
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
                parsed_purchase = parse_hotmart_purchase_payload(
                    payload,
                    config=(
                        settings.commercial_ally_config
                        if settings.portable_hotmart_purchase_stop_enabled
                        else None
                    ),
                )
                if parsed_purchase is None:
                    response.status_code = status.HTTP_200_OK
                    return {
                        "status": "ignored",
                        "reason": "invalid_purchase_payload",
                    }
                if sck_carries_hermes_issuance(parsed_purchase.origin_sck):
                    checkout_admission = (
                        await shared_supabase.admit_and_correlate_hotmart_checkout_issuance_v2(
                            external_event_id=event_id,
                            payload=payload,
                            sck_value=parsed_purchase.origin_sck,
                            now=datetime.now(UTC).isoformat(),
                        )
                    )
                    if (
                        checkout_admission.admission_outcome == "semantic_conflict"
                        or checkout_admission.correlation_outcome
                        in {
                            "conflict",
                            "invalid_hermes_sck",
                            "not_found",
                            "invalid_purchase_event",
                            "purchase_already_approved",
                        }
                    ):
                        response.status_code = status.HTTP_200_OK
                        return {
                            "status": "conflict",
                            "event_id": event_id,
                            "reason": (
                                "checkout_issuance_"
                                f"{checkout_admission.correlation_outcome}"
                            ),
                        }
                    if checkout_admission.admission_outcome == "duplicate":
                        response.status_code = status.HTTP_200_OK
                        return {
                            "status": "duplicate",
                            "event_id": event_id,
                        }
                    return {
                        "status": "received",
                        "event_id": event_id,
                    }
                purchase_admission = (
                    await shared_supabase.admit_portable_hotmart_purchase_approved(
                        config=settings.commercial_ally_config,
                        external_event_id=event_id,
                        payload=payload,
                        normalized_email=parsed_purchase.buyer_email,
                        normalized_phone=parsed_purchase.buyer_phone,
                    )
                    if settings.portable_hotmart_purchase_stop_enabled
                    else await shared_supabase.admit_and_correlate_hotmart_purchase_approved(
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
            parsed_abandonment = parse_hotmart_payload(
                payload,
                config=(
                    settings.commercial_ally_config
                    if settings.portable_hotmart_recovery_enabled
                    else None
                ),
            )
            if parsed_abandonment is None:
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "ignored",
                    "reason": "invalid_cart_abandonment_payload",
                }
            abandonment_admission = (
                await shared_supabase.admit_portable_hotmart_cart_abandonment(
                    config=settings.commercial_ally_config,
                    external_event_id=event_id,
                    payload=payload,
                    normalized_email=parsed_abandonment.buyer_email,
                    normalized_phone=parsed_abandonment.buyer_phone,
                )
                if settings.portable_hotmart_recovery_enabled
                else await shared_supabase.admit_and_correlate_hotmart_cart_abandonment(
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
        except SupabasePermanentError as exc:
            # El evento no es procesable para este sistema y reenviarlo sin
            # cambios nunca puede entrar. Un 503 le dice al emisor que el
            # servicio esta caido y que reintente, y Hotmart cuenta esas fallas
            # para desactivar la configuracion del webhook. Un descarte
            # explicito deja el motivo registrado sin consumir ese contador.
            reason = exc.reason or "rejected_by_contract"
            logger.info(
                "hotmart_event_rejected event_id=%s reason=%s",
                event_id,
                reason,
            )
            response.status_code = status.HTTP_200_OK
            return {
                "status": "ignored",
                "event_id": event_id,
                "reason": reason,
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


class SupabaseTurnProvenanceRecorder:
    """Anota en la capa durable con que prompt contesto el agente.

    Vive aca y no en `bridge.agent_provenance` porque ese modulo es puro y
    `bridge.supabase` ya lo importa: ponerlo alla cerraria el circulo.
    """

    def __init__(
        self,
        *,
        client: SupabaseClient,
        tenant_ref: str,
        scope_ref: str,
        bridge_release: str,
    ) -> None:
        self._client = client
        self._tenant_ref = tenant_ref
        self._scope_ref = scope_ref
        self._bridge_release = bridge_release

    async def record_agent_turn(self, *, turn: AgentTurn) -> None:
        result = await self._client.record_agent_turn_provenance(
            tenant_ref=self._tenant_ref,
            scope_ref=self._scope_ref,
            turn=turn,
            occurred_at=datetime.now(UTC).isoformat(),
            bridge_release=self._bridge_release,
            context_builder_version=CONTEXT_BUILDER_VERSION,
        )
        if not result.attributed:
            # No es una falla, pero tampoco puede pasar inadvertido: si el
            # registrador del perfil no corre, TODOS los turnos salen asi y
            # el feedback vuelve a quedar sin version del prompt.
            logger.warning(
                "agent_turn_provenance_without_release conversation=%s outcome=%s",
                turn.external_conversation_id,
                result.outcome,
            )


def build_app() -> FastAPI:
    """Uvicorn application factory using environment configuration."""
    settings = Settings.from_env()
    shadow_processor: ShadowProcessor | None = None
    if settings.hermes_shadow_enabled:
        if settings.hermes_api_base_url is None or settings.hermes_api_key is None:
            raise ValueError("Hermes shadow settings are incomplete")
        # Sin conocimiento de instancia el procesador se arma igual que siempre:
        # el pedido a Hermes de un runtime sin manifiesto no cambia.
        knowledge_kwargs = (
            {"system_prompt": settings.commercial_knowledge.render()}
            if settings.commercial_knowledge is not None
            else {}
        )
        shadow_processor = HermesShadowProcessor(
            base_url=settings.hermes_api_base_url,
            api_key=settings.hermes_api_key,
            model_name=settings.hermes_model_name,
            shadow_dir=settings.shadow_dir,
            **knowledge_kwargs,
        )
    return create_app(settings, shadow_processor=shadow_processor)
