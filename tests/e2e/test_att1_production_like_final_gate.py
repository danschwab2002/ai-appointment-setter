from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import httpx
import uvicorn

from bridge.app import Settings, create_app
from bridge.commercial_ally import CommercialAllyConfig
from bridge.messaging import FinalMetaEffectGate, FirstTouchResult, WhatsAppTemplateConfig
from bridge.recovery_agent import FollowupMessageProposal
from bridge.supabase import (
    ChatwootAuthorityContext,
    DeliveryAttempt,
    FollowupExecutionContext,
    PilotBoundaryConfig,
    PortablePaymentFailureAdmissionResult,
    ReevaluationDecision,
    ScheduledAction,
)
from bridge.worker import DurableDispatcher, ResolutionWorker


@contextmanager
def _real_http_server(app: object) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on")  # type: ignore[arg-type]
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    try:
        for _ in range(100):
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.1)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
        else:
            raise AssertionError("HTTP server did not become healthy")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        if thread.is_alive():
            raise AssertionError("HTTP server did not stop")


class _Authority:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None
        self.sender_calls = 0
        self.request_start_calls = 0
        self.finalizations: list[dict[str, object]] = []
        self.action = ScheduledAction(
            action_id="action-payment-e2e",
            recovery_case_id="case-payment-e2e",
            followup_sequence_id="sequence-payment-e2e",
            action_type="first_contact_review",
            status="pending",
            due_at="2026-09-03T14:00:00+00:00",
            expires_at="2026-09-10T14:00:00+00:00",
            expected_case_version=1,
            policy_key="att1-payment-failure",
            policy_version=1,
            step_key="payment_failure_first_contact",
            anchor_type="payment_failure",
            anchor_subject_internal_id="event-payment-e2e",
            anchor_observed_at="2026-09-03T13:59:00+00:00",
            lease_owner="payment-e2e",
            lease_generation=1,
            lease_expires_at="2026-09-03T14:05:00+00:00",
            idempotency_key="payment_failure:first_contact:case-payment-e2e",
        )
        self.attempt = DeliveryAttempt(
            attempt_id="attempt-payment-e2e",
            action_id=self.action.action_id,
            idempotency_key=self.action.idempotency_key,
            attempt_number=1,
            channel="whatsapp",
            mode="approved_template",
            phase="reserved",
            lease_generation=1,
            expected_case_version=1,
            expected_sequence_revision=1,
        )
        self.reevaluations = 0

    async def admit_portable_hotmart_payment_failure(self, **kwargs: object) -> PortablePaymentFailureAdmissionResult:
        self.payload = kwargs["payload"]  # type: ignore[assignment]
        return PortablePaymentFailureAdmissionResult(
            outcome="inserted",
            webhook_event_id="event-payment-e2e",
        )

    async def find_contact_by_email(self, _value: str) -> None:
        return None

    async def find_contact_by_phone(self, _value: str) -> None:
        return None

    async def create_contact(self, **_: object) -> str:
        return "contact-payment-e2e"

    async def create_contact_point(self, **_: object) -> None:
        return None

    async def plan_payment_failure_recovery(self, **_: object) -> object:
        return SimpleNamespace(
            created=True,
            recovery_case_id="case-payment-e2e",
        )

    async def fetch_conversations(self, **_: object) -> list[object]:
        return []

    async def fetch_recovery_cases(self, **_: object) -> list[object]:
        return []

    async def fetch_channel_identities(self, **_: object) -> list[object]:
        return []

    async def update_event_status(self, **_: object) -> None:
        return None

    async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
        return [self.action]

    async def get_followup_chatwoot_context(self, **_: object) -> ChatwootAuthorityContext:
        return ChatwootAuthorityContext(
            action_id=self.action.action_id,
            action_type=self.action.action_type,
            chatwoot_account_id=None,
            external_conversation_id=None,
            expected_inbox_id=None,
            anchor_external_message_id=None,
        )

    async def reevaluate_followup_action(self, **_: object) -> ReevaluationDecision:
        self.reevaluations += 1
        return ReevaluationDecision(
            action_id=self.action.action_id,
            decision="execute",
            reason_code="eligible_for_execution",
            case_version=1,
            sequence_revision=1,
        )

    async def reserve_followup_delivery_attempt(self, **_: object) -> DeliveryAttempt:
        return self.attempt

    async def get_followup_execution_context(self, **_: object) -> FollowupExecutionContext:
        return FollowupExecutionContext(
            action_id=self.action.action_id,
            action_type=self.action.action_type,
            step_key=self.action.step_key,
            recovery_case_id=self.action.recovery_case_id,
            contact_id="contact-payment-e2e",
            source_event_id="event-payment-e2e",
            buyer_name="Buyer Test",
            buyer_email="buyer@example.test",
            buyer_phone="12025550124",
            product_name="Alimenta Tu Tiroides",
            offer_code="83utgyow",
            current_goal="recover_payment",
            lead_stage="payment_failed",
        )

    async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
        self.finalizations.append(kwargs)
        return SimpleNamespace(status="retryable_failed")

    async def mark_followup_request_started(self, **_: object) -> DeliveryAttempt:
        self.request_start_calls += 1
        raise AssertionError("closed final gate must precede request_started")


class _Agent:
    async def request_followup_message(self, **_: object) -> FollowupMessageProposal:
        return FollowupMessageProposal(
            strategy="payment_recovery",
            message="Meta-controlled approved template",
        )


class _Sender:
    def __init__(self, authority: _Authority) -> None:
        self._authority = authority

    async def send_first_touch(self, **_: object) -> FirstTouchResult:
        self._authority.sender_calls += 1
        raise AssertionError("closed final gate must prevent sender invocation")


ALLY = CommercialAllyConfig(
    tenant_ref="att1",
    funnel_ref="att1-main",
    binding_version=1,
    ally_ref="att1",
    lead_ally_name="ATT1",
    lead_site="raizana",
    lead_landing_id="inscribirme-alimenta-tu-tiroides",
    lead_page_host="raizana.com.mx",
    lead_page_path="/inscribirme-alimenta-tu-tiroides",
    product_hotlink="D98014973Y",
    product_name="Alimenta Tu Tiroides",
    product_price=Decimal("47"),
    currency="USD",
    offer_code="83utgyow",
    consent_copy_version="att1-consent-v1",
    hotmart_product_id=5071808,
    chatwoot_account_id=42,
    chatwoot_inbox_id=24,
    inbound_scope_key="att1-inbound",
    inbound_scope_version=1,
)


def test_payment_failure_crosses_real_http_and_stops_only_at_final_meta_gate(tmp_path: Path) -> None:
    authority = _Authority()
    settings = Settings(
        webhook_secret="chatwoot-test",
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        commercial_ally_config=ALLY,
        commercial_ally_manifest_path=Path("/runtime/att1.json"),
        portable_hotmart_purchase_stop_enabled=True,
        portable_hotmart_payment_failure_enabled=True,
        hotmart_hottok="test-hottok",
        worker_enabled=False,
    )
    app = create_app(settings, supabase_client=authority)  # type: ignore[arg-type]
    payload = {
        "id": "att1-payment-failure-http-e2e",
        "creation_date": int(time.time() * 1000),
        "event": "PURCHASE_CANCELED",
        "version": "2.0.0",
        "data": {
            "purchase": {
                "transaction": "HPATT1123456",
                "status": "CANCELED",
                "offer": {"code": "83utgyow"},
                "payment": {"refusal_reason": "NO_FUNDS"},
            },
            "product": {"id": 5071808, "name": "Alimenta Tu Tiroides"},
            "buyer": {
                "name": "Synthetic Buyer",
                "email": " Buyer@Example.test ",
                "phone": "+1 (202) 555-0123",
            },
            "checkout_country": {"iso": "MX", "name": "México"},
        },
    }

    with _real_http_server(app) as base_url:
        response = httpx.post(
            f"{base_url}/webhooks/hotmart",
            headers={"X-HOTMART-HOTTOK": "test-hottok"},
            json=payload,
            timeout=5,
        )
    assert response.status_code == 202, response.text
    assert authority.payload == payload

    boundary = PilotBoundaryConfig(
        scope_key="att1-payment-failure",
        scope_version=1,
        tenant_key="att1",
        channel_provider="waba",
        channel_account_ref="chatwoot-inbox:24",
    )
    resolver = ResolutionWorker(
        supabase=authority,  # type: ignore[arg-type]
        policy_key="att1-payment-failure",
        policy_version=1,
        pilot_boundary=boundary,
        commercial_ally_config=ALLY,
        payment_failure_enabled=True,
    )
    import asyncio
    asyncio.run(resolver._process_one({
        "id": "event-payment-e2e",
        "event_type": "PURCHASE_CANCELED",
        "payload": payload,
        "attempt_count": 0,
    }))

    dispatcher = DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="payment-e2e",
        recovery_agent=_Agent(),  # type: ignore[arg-type]
        sender=_Sender(authority),  # type: ignore[arg-type]
        allowed_jid=None,
        portable_recipient_enabled=True,
        commercial_ally_config=ALLY,
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        pilot_boundary=boundary,
        clock=lambda: "2026-09-03T14:01:00+00:00",
        final_meta_effect_gate=FinalMetaEffectGate(
            enabled=False,
            evidence_dir=tmp_path / "meta-effects",
        ),
        waba_template=WhatsAppTemplateConfig(
            first_touch_name="att1_carrito_abandonado_01",
            payment_failure_name="att1_compra_fallida_01",
            followup_name=None,
            language="es_MX",
            category="MARKETING",
            first_touch_parameter="buyer_name_and_product",
        ),
    )
    decisions = asyncio.run(
        dispatcher.dispatch_due(now="2026-09-03T14:00:00+00:00")
    )

    assert decisions[-1].decision == "execute"
    assert authority.request_start_calls == 0
    assert authority.sender_calls == 0
    assert authority.finalizations[-1]["outcome"] == "failed_before_request"
    assert authority.finalizations[-1]["reason_code"] == "final_meta_gate_closed"
    evidence_files = list((tmp_path / "meta-effects").glob("*.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text())
    assert evidence["template_name"] == "att1_compra_fallida_01"
    assert evidence["status"] == "final_meta_gate_closed"
    assert "Buyer Test" not in evidence_files[0].read_text()
