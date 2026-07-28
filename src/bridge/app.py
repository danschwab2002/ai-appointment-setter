"""ASGI application for the Chatwoot webhook bridge."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.filtering import classify_chatwoot_event
from bridge.security import verify_chatwoot_signature


class ChatwootControl(Protocol):
    async def ensure_conversation_label(
        self, *, conversation_id: int, label: str
    ) -> bool: ...


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
    chatwoot_pause_macro_id: int | None = None

    @classmethod
    def from_env(cls) -> Settings:
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
            chatwoot_pause_macro_id=int(os.environ["CHATWOOT_PAUSE_MACRO_ID"]),
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


def create_app(
    settings: Settings,
    *,
    chatwoot_client: ChatwootControl | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Appointment Setter Bridge")
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
            pause_macro_id=settings.chatwoot_pause_macro_id,
        )

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

    return app


def build_app() -> FastAPI:
    """Uvicorn application factory using environment configuration."""
    return create_app(Settings.from_env())
