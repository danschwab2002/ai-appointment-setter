"""ASGI application for the Chatwoot webhook bridge."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, Protocol
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

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.filtering import classify_chatwoot_event
from bridge.hermes import HermesShadowProcessor
from bridge.hotmart import (
    EVENT_CART_ABANDONMENT,
    classify_hotmart_event,
    is_stale_event,
    verify_hotmart_token,
)
from bridge.security import verify_chatwoot_signature
from bridge.supabase import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)
from bridge.messaging import EvolutionMessageSender, MessageSender
from bridge.recovery_agent import RecoveryAgentClient
from bridge.worker import DurableDispatcher, ResolutionWorker


class ChatwootControl(Protocol):
    async def get_conversation_messages(
        self, *, conversation_id: int, limit: int = 20
    ) -> list[dict[str, object]]: ...

    async def ensure_conversation_label(
        self, *, conversation_id: int, label: str
    ) -> bool: ...

    async def send_agent_bot_reply(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        delivery_id: str,
        content: str,
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
    hermes_shadow_enabled: bool = False
    hermes_api_base_url: str | None = None
    hermes_api_key: str | None = None
    hermes_model_name: str = "agente-comercial"
    shadow_dir: Path = Path("./data/shadow")
    automated_replies_enabled: bool = False
    reply_dir: Path = Path("./data/replies")
    hotmart_hottok: str | None = None
    hotmart_max_age_seconds: int = 300
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

    @classmethod
    def from_env(cls) -> Settings:
        shadow_enabled = os.getenv("HERMES_SHADOW_ENABLED", "false").lower() == "true"
        automated_replies_enabled = (
            os.getenv("CHATWOOT_AUTOMATED_REPLIES_ENABLED", "false").lower()
            == "true"
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
            hermes_shadow_enabled=shadow_enabled,
            hermes_api_base_url=hermes_api_base_url,
            hermes_api_key=hermes_api_key,
            hermes_model_name=hermes_model_name,
            shadow_dir=Path(os.getenv("SHADOW_DIR", "./data/shadow")),
            automated_replies_enabled=automated_replies_enabled,
            reply_dir=Path(os.getenv("REPLY_DIR", "./data/replies")),
            hotmart_hottok=hotmart_hottok,
            hotmart_max_age_seconds=hotmart_max_age_seconds,
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
        )


def _capture_payload(
    *, capture_dir: Path, delivery_id: str, payload: dict[str, object]
) -> bool:
    capture_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    capture_path = capture_dir / f"{digest}.json"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    def private_file(path: str, flags: int) -> int:
        return os.open(path, flags, 0o600)

    try:
        with open(capture_path, "x", encoding="utf-8", opener=private_file) as handle:
            handle.write(serialized)
    except FileExistsError:
        return False
    return True


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
            normalized.append(normalized_message)
    return normalized


def create_app(
    settings: Settings,
    *,
    chatwoot_client: ChatwootControl | None = None,
    shadow_processor: ShadowProcessor | None = None,
    supabase_client: SupabaseClient | None = None,
    recovery_agent_client: RecoveryAgentClient | None = None,
    message_sender: MessageSender | None = None,
) -> FastAPI:
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
            and settings.messaging_channel == "evolution"
            and settings.chatwoot_inbox_id is not None
            and control_client is not None
            and isinstance(control_client, ChatwootClient)
        ):
            sender = EvolutionMessageSender(
                chatwoot=control_client,
                inbox_id=settings.chatwoot_inbox_id,
                allowed_jid=settings.allowed_jid,
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
                and settings.messaging_channel == "evolution"
                and settings.chatwoot_inbox_id is not None
                and isinstance(control_client, ChatwootClient)
            ):
                outbound_sender = EvolutionMessageSender(
                    chatwoot=control_client,
                    inbox_id=settings.chatwoot_inbox_id,
                    allowed_jid=settings.allowed_jid,
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
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        if resolution_worker is not None:
            await resolution_worker.start()
        if durable_dispatcher is not None:
            await durable_dispatcher.start()
        yield
        if durable_dispatcher is not None:
            await durable_dispatcher.stop()
        if resolution_worker is not None:
            await resolution_worker.stop()

    app = FastAPI(title="AI Appointment Setter Bridge", lifespan=lifespan)
    app.state.resolution_worker = resolution_worker
    app.state.durable_dispatcher = durable_dispatcher

    async def run_shadow_with_canonical_history(
        *,
        delivery_id: str,
        current_message_id: int | None,
        context: dict[str, object],
    ) -> dict[str, object] | None:
        if control_client is None or settings.agent_bot_id is None:
            if shadow_processor is not None:
                shadow_processor.record_failure(
                    delivery_id=delivery_id,
                    reason="chatwoot_history_not_configured",
                )
            return None
        conversation_id = int(str(context["conversation_ref"]))
        try:
            history = await control_client.get_conversation_messages(
                conversation_id=conversation_id,
                limit=20,
            )
        except (httpx.HTTPError, ChatwootProtocolError):
            if shadow_processor is not None:
                shadow_processor.record_failure(
                    delivery_id=delivery_id,
                    reason="chatwoot_history_unavailable",
                )
            return None
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
            if shadow_processor is not None:
                shadow_processor.record_failure(
                    delivery_id=delivery_id,
                    reason="current_message_not_in_canonical_history",
                )
            return None
        normalized = normalized[: current_index + 1]
        public_messages = [
            {
                "actor": message["actor"],
                "text": message["text"],
            }
            for message in normalized
        ]
        enriched_context = {**context, "messages": public_messages}
        if shadow_processor is None:
            return None
        await shadow_processor.run(
            delivery_id=delivery_id,
            context=enriched_context,
        )
        return shadow_processor.get_completed_proposal(delivery_id=delivery_id)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/chatwoot", status_code=status.HTTP_202_ACCEPTED)
    async def receive_chatwoot_webhook(
        request: Request,
        response: Response,
        x_chatwoot_signature: str = Header(),
        x_chatwoot_timestamp: str = Header(),
        x_chatwoot_delivery: str = Header(),
    ) -> dict[str, object]:
        raw_body = await request.body()
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

        decision = classify_chatwoot_event(
            payload,
            allowed_jid=settings.allowed_jid,
            agent_bot_id=settings.agent_bot_id,
        )
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
            try:
                changed = await control_client.ensure_conversation_label(
                    conversation_id=conversation_id,
                    label="automation_paused",
                )
            except (httpx.HTTPError, ChatwootProtocolError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="chatwoot_control_unavailable",
                ) from exc
            return {
                "status": "automation_paused",
                "reason": decision.reason,
                "label_status": "added" if changed else "already_present",
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
        completed_proposal = (
            shadow_processor.get_completed_proposal(
                delivery_id=x_chatwoot_delivery
            )
            if shadow_processor is not None
            else None
        )
        shadow_pending = (
            shadow_processor is not None
            and context is not None
            and not shadow_processor.has_result(delivery_id=x_chatwoot_delivery)
        )
        reply_pending = (
            settings.automated_replies_enabled and completed_proposal is not None
        )
        if not captured and not shadow_pending and not reply_pending:
            response.status_code = status.HTTP_200_OK
            return {
                "status": "duplicate",
                "delivery_id": x_chatwoot_delivery,
            }
        if shadow_pending and context is not None:
            message_id = payload.get("id")
            completed_proposal = await run_shadow_with_canonical_history(
                delivery_id=x_chatwoot_delivery,
                current_message_id=(
                    message_id
                    if isinstance(message_id, int) and not isinstance(message_id, bool)
                    else None
                ),
                context=context,
            )
        if settings.automated_replies_enabled and completed_proposal is not None:
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
                raise HTTPException(
                    status_code=503,
                    detail="chatwoot_reply_not_configured",
                )
            try:
                reply_result = await control_client.send_agent_bot_reply(
                    conversation_id=conversation_id,
                    trigger_message_id=message_id,
                    delivery_id=x_chatwoot_delivery,
                    content=reply,
                )
            except (httpx.HTTPError, ChatwootProtocolError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="chatwoot_reply_unavailable",
                ) from exc
            reply_status = reply_result.get("status")
            if reply_status in {"sent", "duplicate"}:
                return {
                    "status": (
                        "reply_sent" if reply_status == "sent" else "reply_duplicate"
                    ),
                    "delivery_id": x_chatwoot_delivery,
                    "message_id": reply_result.get("message_id"),
                }
            if reply_status == "blocked":
                return {
                    "status": "reply_blocked",
                    "delivery_id": x_chatwoot_delivery,
                    "reason": reply_result.get("reason"),
                }
            raise HTTPException(
                status_code=503,
                detail="invalid_chatwoot_reply_result",
            )
        if shadow_pending:
            return {
                "status": "shadow_processed",
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
        raw_body = await request.body()

        if settings.hotmart_hottok is None:
            raise HTTPException(
                status_code=503, detail="hotmart_not_configured"
            )
        if not verify_hotmart_token(
            received_token=x_hotmart_hottok,
            expected_token=settings.hotmart_hottok,
        ):
            raise HTTPException(status_code=401, detail="invalid_token")

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
            result = await shared_supabase.insert_webhook_event(
                source="hotmart",
                external_event_id=event_id,
                event_type=EVENT_CART_ABANDONMENT,
                payload=payload,
            )
        except SupabaseError as exc:
            raise HTTPException(
                status_code=503, detail="webhook_persist_unavailable"
            ) from exc

        if not result.inserted:
            response.status_code = status.HTTP_200_OK
            return {
                "status": "duplicate",
                "event_id": event_id,
            }
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
