from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import uvicorn

from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient
from bridge.commercial_ally import CommercialAllyConfig
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_manifest import GhlRiskAcceptance, InstanceManifest
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

    # The portable resolution looks the contact up by the equivalent forms of
    # the phone (52/521, 54/549), never by the exact one.
    async def find_contact_by_phones(self, _values: tuple[str, ...]) -> None:
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

    # NOTE: this fake answers "execute" without running the real
    # reevaluate_followup_action, so this test cannot see a rejection that only
    # the database makes (channel_mode_unsupported for a non-freeform policy
    # step, contact_authorization_unknown for a payment failure without a
    # permission). The real reevaluation of the ATT1 chain is covered by
    # tests/sql/followup_engine/validate_att1_portable_chain.mjs.
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


# --------------------------- el primer contacto tras el formulario (1.3.0)
#
# El mismo recorrido para el flujo nuevo, con el runtime armado por create_app:
#   el envio real de GHL cruza HTTP -> el adaptador lo traduce -> una sola RPC
#   admite y planifica (admit_and_plan_portable_lead_precheckout) -> el
#   dispatcher de create_app toma la accion de ancla precheckout_intent ->
#   reevalua con ese ancla -> lee el catalogo del inbox -> resuelve el contacto
#   de Chatwoot ANTES del gate -> y frena en el gate final cerrado, que deja en
#   la evidencia el wa_id que Meta recibiria, sin arrancar el pedido ni mandar.
#
# Datos:
#   - tests/fixtures/instances/att1/: el manifiesto de ATT1, con lo que la
#     instancia va a tener al prender el flujo (precheckout e inbound en true,
#     el evento intencion y [adaptadores.ghl] con el formulario de ads-a). La
#     aceptacion del riesgo es DE PRUEBA: ningun manifiesto real la recibe de
#     este codigo.
#   - tests/fixtures/ghl/ghl_form_webhook_att1_ads_a_20260929.json: el envio
#     capturado de GHL (anonimizado), con sus headers. Es un movil argentino:
#     el formulario lo guarda como 54 + 10 digitos.
#   - tests/fixtures/chatwoot_inbox_11_message_templates_20261001.json: el
#     catalogo capturado del inbox 11, con att1_interes_precheckout_01.
#   - Chatwoot: no hay captura de /contacts/search ni de GET /inboxes/11; es el
#     precedente inline de tests/test_durable_dispatcher_approved_template.py.
#     El contacto que ya existe es el de quien escribio antes por WhatsApp: su
#     source_id es el wa_id (549 + 10 digitos), medido el 2026-10-01.
#   - La base es un doble: contesta "execute" sin correr las RPC reales. La
#     reevaluacion y el arranque reales del primer contacto los recorre
#     tests/sql/followup_engine/validate_att1_portable_chain.mjs (caso 9).

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_ATT1_INSTANCE = _FIXTURES / "instances" / "att1"
_GHL_CAPTURE = json.loads(
    (_FIXTURES / "ghl" / "ghl_form_webhook_att1_ads_a_20260929.json").read_text(
        encoding="utf-8"
    )
)
_INBOX_11_CATALOG: list[dict[str, Any]] = json.loads(
    (_FIXTURES / "chatwoot_inbox_11_message_templates_20261001.json").read_text(
        encoding="utf-8"
    )
)["templates"]
_GHL_ADS_A_FORM = "EgDqRl2xWc59YjVW1q8W"
_GHL_TOKEN = "ghl-adapter-test-token-0123456789abcdef"
_FORM_TEMPLATE = "att1_interes_precheckout_01"
_FORM_SCOPE = "att1-primer-contacto"


def _first_contact_settings(tmp_path: Path) -> Settings:
    """El set con el que la instancia prende el primer contacto, gate cerrado."""
    instance = tmp_path / "instancia"
    shutil.copytree(_ATT1_INSTANCE, instance)
    knowledge_path = instance / "conocimiento" / "knowledge-v1.toml"
    knowledge_path.write_text(
        knowledge_path.read_text(encoding="utf-8").replace(
            'estado = "borrador"',
            'estado = "aprobado"\naprobado_por = "test"\naprobado_el = 2026-09-28',
        ),
        encoding="utf-8",
    )
    manifest = InstanceManifest.from_toml_file(instance / "instancia.toml")
    manifest = replace(
        manifest,
        flows={**manifest.flows, "precheckout": True, "inbound": True},
        events=frozenset(manifest.events | {"intencion"}),
        ghl_form_ids=(_GHL_ADS_A_FORM,),
        ghl_risk_acceptance=GhlRiskAcceptance(
            accepted_by="aceptacion de prueba (tests)",
            accepted_on=date(2026, 10, 1),
            contract="ghl-precheckout-adapter-v1",
        ),
    )
    config = manifest.to_commercial_ally_config()
    return Settings(
        webhook_secret="chatwoot-test",
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        commercial_ally_config=config,
        commercial_ally_manifest_path=instance / "instancia.toml",
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        commercial_knowledge=CommercialKnowledge.from_toml_file(knowledge_path),
        agent_bot_id=1,
        chatwoot_account_id=config.chatwoot_account_id,
        chatwoot_inbox_id=config.chatwoot_inbox_id,
        # La entrada del formulario: el adaptador de GHL.
        ghl_precheckout_adapter_enabled=True,
        ghl_precheckout_adapter_token=_GHL_TOKEN,
        # El envio: frontera del piloto, dispatcher y plantilla aprobada directa.
        pilot_boundary_enabled=True,
        pilot_scope_key="att1-recuperacion",
        pilot_scope_version=1,
        pilot_tenant_key=config.tenant_ref,
        pilot_channel_provider="waba",
        pilot_channel_account_ref=f"chatwoot-inbox:{config.chatwoot_inbox_id}",
        dispatcher_enabled=True,
        dispatcher_worker_id="att1-dispatcher",
        dispatcher_outbound_enabled=True,
        dispatcher_approved_template_direct_enabled=True,
        waba_first_touch_template_name="att1_carrito_abandonado_01",
        waba_template_language="es_MX",
        waba_template_category="MARKETING",
        portable_precheckout_first_contact_enabled=True,
        pilot_precheckout_scope_key=_FORM_SCOPE,
        pilot_precheckout_scope_version=1,
        waba_precheckout_template_name=_FORM_TEMPLATE,
        # El gate final cerrado: es lo ultimo que se prende.
        meta_final_effect_enabled=False,
        meta_final_effect_evidence_dir=tmp_path / "meta-effects",
        # Lo que frena al primer contacto: la compra y el entrante scoped.
        portable_hotmart_purchase_stop_enabled=True,
        hotmart_hottok="test-hottok",
        chatwoot_scoped_inbound_senders_enabled=True,
        chatwoot_cut_b_admission_enabled=True,
        chatwoot_cut_b_scope_key=config.inbound_scope_key,
        chatwoot_cut_b_scope_version=config.inbound_scope_version,
        chatwoot_cut_b_agent_enabled=True,
        automated_replies_enabled=True,
        chatwoot_durable_opt_out_enabled=True,
        chatwoot_opt_out_macro_id=5,
        opt_out_projection_worker_id="att1-opt-out-projection",
        chatwoot_human_pause_enabled=True,
        human_handoff_admission_enabled=True,
        human_handoff_projection_enabled=True,
        handoff_projection_policy_key="att1-derivacion",
        handoff_projection_policy_version=1,
        human_handoff_projection_worker_id="att1-handoff-projection",
    )


class _FirstContactAuthority:
    """La base: admite y planifica el formulario, y despues entrega su accion."""

    def __init__(self) -> None:
        self.plan_calls: list[dict[str, Any]] = []
        self.reevaluation_anchors: list[object] = []
        self.request_start_calls = 0
        self.finalizations: list[dict[str, object]] = []
        # La accion se entrega recien cuando el test la suelta: con el servidor
        # HTTP levantado corren los workers del bridge, y el dispatcher no
        # tiene que encontrar trabajo antes de tiempo.
        self.released = False

    # Los dos workers de proyeccion que arrancan con el servidor leen la base en
    # su primera vuelta. No encuentran trabajo.
    async def claim_chatwoot_opt_out_projections(self, **_: object) -> list[object]:
        return []

    async def claim_human_handoff_projection_effects(self, **_: object) -> list[object]:
        return []

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def admit_and_plan_portable_lead_precheckout(self, **kwargs: Any) -> object:
        self.plan_calls.append(kwargs)
        return SimpleNamespace(
            outcome="inserted",
            submission_id="bfc778e7-5c9f-45e6-a910-651f92312157",
            purchase_intent_id="1f581f3a-c469-45da-8208-9483d1b26f0b",
            plan_outcome="planned",
            plan_reason="first_contact_scheduled",
        )

    def _planned(self) -> dict[str, Any]:
        [call] = self.plan_calls
        return call["canonical_payload"]

    def _action(self) -> ScheduledAction:
        now = datetime.now(UTC)
        return ScheduledAction(
            action_id="action-first-contact-e2e",
            recovery_case_id="case-first-contact-e2e",
            followup_sequence_id="sequence-first-contact-e2e",
            action_type="first_contact_review",
            status="pending",
            due_at=(now - timedelta(minutes=1)).isoformat(),
            expires_at=(now + timedelta(hours=23)).isoformat(),
            expected_case_version=1,
            policy_key="att1-primer-contacto-formulario",
            policy_version=1,
            step_key="first_contact",
            anchor_type="precheckout_intent",
            anchor_subject_internal_id="event-first-contact-e2e",
            anchor_observed_at=(now - timedelta(minutes=61)).isoformat(),
            lease_owner="att1-dispatcher",
            lease_generation=1,
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
            idempotency_key="precheckout_intent:first_contact:case-first-contact-e2e",
        )

    async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
        return [self._action()] if self.released else []

    async def get_followup_chatwoot_context(self, **_: object) -> ChatwootAuthorityContext:
        return ChatwootAuthorityContext(
            action_id="action-first-contact-e2e",
            action_type="first_contact_review",
            chatwoot_account_id=None,
            external_conversation_id=None,
            expected_inbox_id=None,
            anchor_external_message_id=None,
        )

    async def reevaluate_followup_action(self, **kwargs: object) -> ReevaluationDecision:
        self.reevaluation_anchors.append(kwargs.get("anchor_type"))
        return ReevaluationDecision(
            action_id="action-first-contact-e2e",
            decision="execute",
            reason_code="eligible_for_execution",
            case_version=1,
            sequence_revision=1,
        )

    async def reserve_followup_delivery_attempt(self, **kwargs: object) -> DeliveryAttempt:
        assert kwargs["mode"] == "approved_template"
        return DeliveryAttempt(
            attempt_id="attempt-first-contact-e2e",
            action_id="action-first-contact-e2e",
            idempotency_key="precheckout_intent:first_contact:case-first-contact-e2e",
            attempt_number=1,
            channel="whatsapp",
            mode="approved_template",
            phase="reserved",
            lease_generation=1,
            expected_case_version=1,
            expected_sequence_revision=1,
        )

    async def get_followup_execution_context(self, **_: object) -> FollowupExecutionContext:
        # Lo que la base devolveria para el caso que planifico el formulario:
        # el contacto con el telefono, el nombre y la oferta del envio admitido.
        planned = self._planned()
        return FollowupExecutionContext(
            action_id="action-first-contact-e2e",
            action_type="first_contact_review",
            step_key="first_contact",
            recovery_case_id="case-first-contact-e2e",
            contact_id="contact-first-contact-e2e",
            source_event_id="event-first-contact-e2e",
            buyer_name=planned["lead"]["full_name"],
            buyer_email=planned["identity"]["email"],
            buyer_phone=planned["identity"]["phone"],
            product_name=planned["commerce"]["product_name"],
            offer_code=planned["commerce"]["offer_ref"],
            current_goal=None,
            lead_stage="new",
        )

    async def get_lead_first_name_inference(self, _name_key: str) -> None:
        return None

    async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
        self.finalizations.append(kwargs)
        return SimpleNamespace(status="retryable_failed")

    async def mark_followup_request_started(self, **_: object) -> DeliveryAttempt:
        self.request_start_calls += 1
        raise AssertionError("closed final gate must precede request_started")


class _Inbox11:
    """Chatwoot del inbox 11: el catalogo capturado y el contacto que ya existe."""

    def __init__(self, *, account_id: int, inbox_id: int, wa_id: str) -> None:
        self.account_id = account_id
        self.inbox_id = inbox_id
        self.wa_id = wa_id
        self.searches: list[str] = []
        self.writes: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        prefix = f"/api/v1/accounts/{self.account_id}"
        if request.method != "GET":
            self.writes.append((request.method, path))
            return httpx.Response(404, json={"error": "not_emulated"})
        if path == f"{prefix}/inboxes/{self.inbox_id}":
            return httpx.Response(200, json={
                "id": self.inbox_id,
                "message_templates": copy.deepcopy(_INBOX_11_CATALOG),
            })
        if path == f"{prefix}/contacts/search":
            query = request.url.params["q"]
            self.searches.append(query)
            if query != f"+{self.wa_id}":
                return httpx.Response(200, json={"payload": []})
            return httpx.Response(200, json={"payload": [{
                "id": 41,
                "phone_number": query,
                "blocked": False,
                "contact_inboxes": [
                    {"source_id": self.wa_id, "inbox": {"id": self.inbox_id}}
                ],
            }]})
        return httpx.Response(404, json={"error": "not_emulated"})

    def client(self) -> ChatwootClient:
        return ChatwootClient(
            base_url="https://chatwoot.test",
            account_id=self.account_id,
            access_token="control-token",
            agent_bot_access_token="bot-token",
            agent_bot_id=1,
            transport=httpx.MockTransport(self.handler),
        )


class _ShadowProcessor:
    async def run(self, **_: object) -> object:
        raise AssertionError("the first contact does not call Hermes")


def test_first_contact_crosses_real_http_and_stops_only_at_final_meta_gate(
    tmp_path: Path,
) -> None:
    settings = _first_contact_settings(tmp_path)
    authority = _FirstContactAuthority()
    config = settings.commercial_ally_config
    chatwoot = _Inbox11(
        account_id=config.chatwoot_account_id,
        inbox_id=config.chatwoot_inbox_id,
        wa_id="",
    )
    app = create_app(
        settings,
        supabase_client=authority,  # type: ignore[arg-type]
        chatwoot_client=chatwoot.client(),
        shadow_processor=_ShadowProcessor(),  # type: ignore[arg-type]
    )

    with _real_http_server(app) as base_url:
        response = httpx.post(
            f"{base_url}/webhooks/adapters/ghl/lead-precheckout",
            headers={
                "User-Agent": _GHL_CAPTURE["headers"]["User-Agent"],
                "Content-Type": _GHL_CAPTURE["headers"]["Content-Type"],
                "X-Setter-Adapter-Token": _GHL_TOKEN,
            },
            content=json.dumps(
                _GHL_CAPTURE["payload"], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"),
            timeout=5,
        )

    # 1. El envio real de GHL se admitio y se planifico en una sola RPC, contra
    #    el scope del formulario.
    assert response.status_code == 200, response.text
    [call] = authority.plan_calls
    assert (call["scope_key"], call["scope_version"]) == (_FORM_SCOPE, 1)
    assert call["config"] == settings.commercial_ally_config
    canonical = call["canonical_payload"]
    form_phone = canonical["identity"]["phone"]
    assert canonical["consent"]["whatsapp_contact"] is True
    # El formulario guarda el movil argentino como 54 + 10 digitos.
    assert form_phone.startswith("54") and len(form_phone) == 12
    # Con el servidor levantado el dispatcher no encontro trabajo.
    assert authority.finalizations == []
    assert chatwoot.searches == [] and chatwoot.writes == []

    # 2. El dispatcher que armo create_app toma la accion del formulario. El
    #    contacto de Chatwoot de quien ya escribio por WhatsApp tiene el wa_id:
    #    549 + los mismos 10 digitos.
    wa_id = f"549{form_phone[2:]}"
    chatwoot.wa_id = wa_id
    authority.released = True
    decisions = asyncio.run(
        app.state.durable_dispatcher.dispatch_due(  # type: ignore[attr-defined]
            now=datetime.now(UTC).isoformat()
        )
    )

    # 3. Reevaluo con el ancla del primer contacto (la que elige sus RPC
    #    propias) y freno en el gate final, sin arrancar el pedido ni escribir
    #    en Chatwoot.
    assert decisions[-1].decision == "execute"
    assert authority.reevaluation_anchors == ["precheckout_intent", "precheckout_intent"]
    assert authority.request_start_calls == 0
    assert chatwoot.writes == []
    assert authority.finalizations[-1]["outcome"] == "failed_before_request"
    assert authority.finalizations[-1]["reason_code"] == "final_meta_gate_closed"

    # 4. La evidencia del gate: la plantilla del formulario y el wa_id resuelto
    #    (el del contacto de Chatwoot que existe), no el telefono del formulario.
    assert chatwoot.searches == [f"+{form_phone}", f"+{wa_id}"]
    [evidence_file] = list((tmp_path / "meta-effects").glob("*.json"))
    evidence_text = evidence_file.read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)
    assert evidence["template_name"] == _FORM_TEMPLATE
    assert evidence["status"] == "final_meta_gate_closed"
    assert evidence["target_sha256"] == hashlib.sha256(
        f"+{wa_id}".encode("utf-8")
    ).hexdigest()
    assert evidence["target_sha256"] != hashlib.sha256(
        f"+{form_phone}".encode("utf-8")
    ).hexdigest()
    # Ni el nombre ni el numero, en ninguna de sus dos formas.
    assert canonical["lead"]["full_name"] not in evidence_text
    assert form_phone not in evidence_text
    assert wa_id not in evidence_text
    assert form_phone[2:] not in evidence_text

