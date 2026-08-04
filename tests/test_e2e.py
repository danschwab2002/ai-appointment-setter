"""End-to-end test: webhook → resolve → agent → send WhatsApp message.

Simulates the full recovery flow with mock HTTP transports for Supabase,
Hermes (agent), and Chatwoot. No external calls are made.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient
from bridge.messaging import EvolutionMessageSender
from bridge.recovery_agent import RecoveryAgentClient
from bridge.supabase import SupabaseClient


# ── Shared mock transport ────────────────────────────────────────────


class E2ETransport(httpx.AsyncBaseTransport):
    """Single transport that routes all HTTP calls to configurable handlers."""

    def __init__(self) -> None:
        self.routes: list[
            tuple[str, str, list[dict[str, object] | Exception]]
        ] = []
        self.requests: list[tuple[str, str, bytes]] = []

    def set(
        self,
        method: str,
        path_prefix: str,
        responses: list[dict[str, object] | Exception],
    ) -> None:
        self.routes.append((method, path_prefix, responses))

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.requests.append((method, path, request.content))

        for r_method, prefix, responses in self.routes:
            if method == r_method and path.startswith(prefix):
                if not responses:
                    if method == "GET":
                        return httpx.Response(
                            200, content=b"[]", request=request
                        )
                    return httpx.Response(204, request=request)
                item = responses.pop(0)
                if isinstance(item, Exception):
                    raise httpx.ConnectError("mock error", request=request)
                status = int(item.pop("_status", 201))  # type: ignore[union-attr]
                if path.startswith("/rest/v1/"):
                    body = json.dumps([item]) if item else "[]"
                else:
                    body = json.dumps(item) if item else "{}"
                return httpx.Response(
                    status,
                    content=body.encode(),
                    request=request,
                    headers={"Content-Type": "application/json"},
                )
        if method == "GET":
            return httpx.Response(200, content=b"[]", request=request)
        return httpx.Response(204, request=request)


# ── Test data ───────────────────────────────────────────────────────

NOW_MS = int(time.time() * 1000)

HOTMART_PAYLOAD: dict[str, object] = {
    "id": "evt-e2e-001",
    "creation_date": NOW_MS,
    "event": "PURCHASE_OUT_OF_SHOPPING_CART",
    "version": "2.0.0",
    "data": {
        "affiliate": True,
        "product": {"id": 3526906, "name": "IA para empresarios"},
        "buyer": {
            "name": "Juan Perez",
            "email": "juan@example.com",
            "phone": "5531999999999",
        },
        "offer": {"code": "n82b9jqz"},
        "checkout_country": {"name": "Brasil", "iso": "BR"},
    },
}

AGENT_PROPOSAL: dict[str, object] = {
    "action": "send_first_touch",
    "reason_code": "first_touch",
    "message": "¡Hola, Juan! Soy el asistente virtual de Dan. Vi que estabas mirando el curso de IA para empresarios. ¿Te quedó alguna duda?",
    "lead_stage": "new",
    "current_goal": "iniciar conversación de recupero",
}


def _build_app(transport: E2ETransport, tmp_path: Path):
    """Build the app with all dependencies wired to the mock transport."""
    settings = Settings(
        webhook_secret="unused",
        allowed_jid="5531999999999@s.whatsapp.net",
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        hotmart_hottok="e2e-hottok",
        hotmart_max_age_seconds=300,
        supabase_base_url="https://fake.supabase.co",
        supabase_service_role_key="fake-service-role-key",
        worker_enabled=True,
        worker_poll_interval_seconds=0.1,
        worker_batch_size=5,
        followup_policy_key="cart-recovery-test",
        followup_policy_version=1,
        chatwoot_inbox_id=1,
        messaging_channel="evolution",
        hermes_api_base_url="https://hermes.test/v1",
        hermes_api_key="fake-hermes-key",
        hermes_model_name="agente-comercial",
        agent_bot_id=99,
        chatwoot_base_url="https://chatwoot.test",
        chatwoot_account_id=1,
        chatwoot_control_api_access_token="test-control-token",
        chatwoot_agent_bot_access_token="test-bot-token",
        chatwoot_pause_macro_id=1,
    )

    supabase = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=transport,
    )
    chatwoot = ChatwootClient(
        base_url="https://chatwoot.test",
        account_id=1,
        access_token="test-control-token",
        agent_bot_access_token="test-bot-token",
        agent_bot_id=99,
        transport=transport,
    )
    recovery_agent = RecoveryAgentClient(
        base_url="https://hermes.test/v1",
        api_key="fake-hermes-key",
        model_name="agente-comercial",
        proposals_dir=tmp_path / "recovery",
        transport=transport,
    )
    message_sender = EvolutionMessageSender(
        chatwoot=chatwoot,
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
    )

    app = create_app(
        settings,
        chatwoot_client=chatwoot,
        supabase_client=supabase,
        recovery_agent_client=recovery_agent,
        message_sender=message_sender,
    )
    return app


def _setup_mocks(transport: E2ETransport) -> None:
    """Configure all mock responses for the full flow."""
    # ── Supabase: insert webhook_events ─────────────────────────────
    transport.set("POST", "/rest/v1/webhook_events", [
        {"_status": 201, "id": "we-e2e-001"},
    ])
    # ── Supabase: contact_points lookups (email + phone → empty) ────
    transport.set("GET", "/rest/v1/contact_points", [
        {"_status": 200},
        {"_status": 200},
    ])
    # ── Supabase: create contact ─────────────────────────────────────
    transport.set("POST", "/rest/v1/contacts", [
        {"_status": 201, "id": "contact-e2e-001"},
    ])
    # ── Supabase: create contact_points (email + phone) ─────────────
    transport.set("POST", "/rest/v1/contact_points", [
        {"_status": 201},
        {"_status": 201},
    ])
    # ── Supabase: plan durable recovery ──────────────────────────────
    transport.set("POST", "/rest/v1/rpc/plan_cart_recovery", [
        {
            "_status": 200,
            "recovery_case_id": "rc-e2e-001",
            "followup_sequence_id": "seq-e2e-001",
            "scheduled_action_id": "action-e2e-001",
            "created": True,
        },
    ])
    # ── Supabase: log resolution attempt ────────────────────────────
    transport.set("POST", "/rest/v1/identity_resolution_attempts", [
        {"_status": 201},
    ])
    # ── Supabase: fetch conversations → empty ───────────────────────
    transport.set("GET", "/rest/v1/conversations", [{"_status": 200}])
    # ── Supabase: fetch recovery_cases → empty ──────────────────────
    transport.set("GET", "/rest/v1/recovery_cases", [{"_status": 200}])
    # ── Supabase: fetch channel_identities → empty ──────────────────
    transport.set("GET", "/rest/v1/channel_identities", [{"_status": 200}])
    # ── Supabase: update event status → processed ──────────────────
    transport.set("PATCH", "/rest/v1/webhook_events", [{"_status": 204}])
    # ── Hermes: agent response ──────────────────────────────────────
    transport.set("POST", "/v1/chat/completions", [
        {
            "_status": 200,
            "choices": [
                {
                    "message": {
                        "content": json.dumps(AGENT_PROPOSAL)
                    }
                }
            ],
        },
    ])
    # ── Chatwoot: search then create contact ───────────────────────
    transport.set("GET", "/api/v1/accounts/1/contacts/search", [
        {"_status": 200, "payload": []},
    ])
    transport.set("POST", "/api/v1/accounts/1/contacts", [
        {"_status": 200, "id": 42, "name": "Juan Perez"},
    ])
    # ── Chatwoot: create conversation ───────────────────────────────
    transport.set("POST", "/api/v1/accounts/1/conversations", [
        {"_status": 200, "id": 100, "status": "open"},
    ])
    # ── Chatwoot: send first message ────────────────────────────────
    transport.set("POST", "/api/v1/accounts/1/conversations/100/messages", [
        {
            "_status": 200,
            "id": 999,
            "message_type": 1,
            "private": False,
            "content": AGENT_PROPOSAL["message"],
        },
    ])
    # ── Supabase: fetch pending events (worker poll) ────────────────
    transport.set("GET", "/rest/v1/webhook_events", [
        {
            "_status": 200,
            "id": "we-e2e-001",
            "source": "hotmart",
            "external_event_id": "evt-e2e-001",
            "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
            "payload": HOTMART_PAYLOAD,
        },
    ])


def test_e2e_webhook_to_durable_plan_without_outbound_send(tmp_path: Path) -> None:
    """Hotmart webhook → identity resolution → durable plan, without send."""
    transport = E2ETransport()
    _setup_mocks(transport)
    app = _build_app(transport, tmp_path)
    worker = app.state.resolution_worker
    assert worker is not None

    raw_body = json.dumps(HOTMART_PAYLOAD).encode()

    async def run_e2e():
        # Step 1: POST the Hotmart webhook
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/webhooks/hotmart",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-HOTMART-HOTTOK": "e2e-hottok",
                },
            )
        assert response.status_code == 202
        assert response.json()["status"] == "received"

        # Step 2: Start the worker manually (ASGITransport doesn't run lifespan)
        await worker.start()

        # Step 3: Wait for the worker to process and plan the action
        deadline = time.time() + 5.0
        while time.time() < deadline:
            processed = [
                r for r in transport.requests
                if r[0] == "PATCH" and r[1] == "/rest/v1/webhook_events"
            ]
            if processed:
                break
            await asyncio.sleep(0.1)

        await worker.stop()

        # Step 4: Verify planning completed without an outbound message
        send_msgs = [
            r for r in transport.requests
            if r[0] == "POST" and "/conversations/100/messages" in r[1]
        ]
        assert len(send_msgs) == 0, "Durable planning must not send immediately"

        plan_posts = [
            r for r in transport.requests
            if r[0] == "POST" and r[1] == "/rest/v1/rpc/plan_cart_recovery"
        ]
        assert len(plan_posts) == 1

        # Verify the webhook event was marked as processed
        patch_requests = [
            r for r in transport.requests if r[0] == "PATCH"
        ]
        assert len(patch_requests) >= 1

        # Hermes and Chatwoot must not run before the due-action dispatcher.
        agent_posts = [
            r for r in transport.requests
            if r[0] == "POST" and "/chat/completions" in r[1]
        ]
        assert len(agent_posts) == 0

    asyncio.run(run_e2e())
