"""Paquete identificado de revision diaria (v2, ADR-0018).

Los payloads de Chatwoot son capturas reales del inbox 9 de Johanna del
26/09/2026 (``tests/fixtures/chatwoot_daily_feedback_*_20260926.json``), con
la identidad de los leads reemplazada. Lo que se prueba es que el revisor vea
lo mismo que veria en Chatwoot: quien es el lead, cuando escribio, que mando el
sistema por su cuenta y por que el agente decidio lo que decidio.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.daily_feedback import minimize_review_text_v2, sanitize_review_text
from bridge.daily_feedback_app import (
    RENDERER_VERSION_V2,
    DailyFeedbackRuntimeSettings,
)
from bridge.daily_feedback_export import (
    PACKAGE_SCHEMA_V1,
    PACKAGE_SCHEMA_V2,
    SANITIZER_VERSION_V2,
    SELECTION_VERSION_V2,
    ChatwootDailyCollector,
    DailyReviewPackage,
    RealConversationSecurityPolicy,
    ReviewConversation,
    ReviewMessage,
    apply_conversation_context,
    classify_chatwoot_message,
    derive_apparent_objective,
    derive_observed_outcome,
)
from bridge.daily_feedback_service import (
    DailyFeedbackScheduler,
    DailyFeedbackSchedulerSettings,
    DailyFeedbackWebSettings,
    SupabaseDailyFeedbackRepository,
    _package_items,
    _review_page,
)

from test_chatwoot import ALLOWED_JID, AuthorizedConversationTransport
from test_daily_feedback_service import Clock, FakeProducer, WorkflowRepository


FIXTURES = Path(__file__).parent / "fixtures"
CONVERSATIONS = json.loads(
    (FIXTURES / "chatwoot_daily_feedback_conversations_20260926.json").read_text("utf-8")
)
MESSAGES = json.loads(
    (FIXTURES / "chatwoot_daily_feedback_messages_20260926.json").read_text("utf-8")
)
WINDOW_START = datetime(2026, 9, 24, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 27, tzinfo=UTC)
CHATWOOT_URL = "https://chatwoot.example.test"


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _bogota(epoch: int) -> str:
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(epoch, tz=UTC).astimezone(ZoneInfo("America/Bogota")).strftime("%d/%m %H:%M")


def _fixture_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/accounts/1/conversations":
            return httpx.Response(200, json={"data": CONVERSATIONS["data"]})
        if path.startswith("/api/v1/accounts/1/conversations/") and path.endswith("/messages"):
            if request.url.params.get("before") is not None:
                return httpx.Response(200, json={"payload": []})
            conversation_id = path.split("/")[-2]
            return httpx.Response(200, json={"payload": MESSAGES[conversation_id]["payload"]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def _collector(*, package_version: int = 2) -> ChatwootDailyCollector:
    return ChatwootDailyCollector(
        base_url=CHATWOOT_URL,
        account_id=1,
        inbox_id=9,
        agent_bot_id=1,
        access_token="not-a-real-token",
        pseudonymization_key=b"k" * 32,
        security_policy=RealConversationSecurityPolicy(
            real_collection_enabled=True,
            storage_encryption_verified=True,
            retention_hours=72,
            deletion_owner="privacy-operator",
        ),
        transport=_fixture_transport(),
        package_version=package_version,
    )


def _collect_v2() -> DailyReviewPackage:
    return _collector().collect(
        tenant_ref="lancemos",
        scope_ref="psicologajohanna-agent-bot-19",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


def _by_conversation(package: DailyReviewPackage) -> dict[int, ReviewConversation]:
    return {
        int(conversation.context["chatwoot_conversation_id"]): conversation
        for conversation in package.conversations
    }


def test_v2_collects_every_actor_and_labels_templates_links_and_events() -> None:
    package = _collect_v2()

    assert package.schema_version == PACKAGE_SCHEMA_V2
    assert package.sanitizer_version == SANITIZER_VERSION_V2
    assert package.selection_version == SELECTION_VERSION_V2
    assert [c.display_label for c in package.conversations] == [
        "Conversación 01",
        "Conversación 02",
        "Conversación 03",
    ]
    conversations = _by_conversation(package)
    assert list(conversations) == [177, 184, 186]

    # 177: la reactivacion real, el equipo, la nota de derivacion y las etiquetas
    kinds_177 = [(m.actor, m.kind) for m in conversations[177].messages]
    assert kinds_177 == [
        ("prospect", "prospect_message"),
        ("team", "team_message"),
        ("system", "automation_paused"),
        ("prospect", "prospect_message"),
        ("team", "handoff_note"),
        ("system", "assigned"),
        ("agent", "reactivation_template"),
        ("prospect", "prospect_message"),
        ("system", "automation_resumed"),
        ("system", "automation_paused"),
        ("team", "handoff_note"),
    ]
    first, team, paused, *_ = conversations[177].messages
    assert first.text == "[sin texto: adjunto, audio o sticker]"
    assert first.meta["empty"] is True
    assert team.meta["author"] == "Mariana Equipo"
    assert team.status == "read"
    assert paused.meta == {"actor_name": "Bridge Service", "label": "automation_paused"}
    reactivation = conversations[177].messages[6]
    assert reactivation.meta["chatwoot_message_id"] == 2375
    assert "Pablo" in reactivation.text
    assigned = conversations[177].messages[5]
    assert assigned.meta == {"assignee": "johanna - revisión humana", "actor_name": "Bridge Service"}
    resumed = conversations[177].messages[8]
    assert resumed.meta["actor_name"] == "Dan Operador"
    note = conversations[177].messages[4]
    assert note.text.startswith("Derivación inbound registrada por el bridge.")
    assert "\n" in note.text  # el salto de linea del motivo se conserva

    # 186: las tres partes de una respuesta, con el marcador de parte
    parts = [m for m in conversations[186].messages if m.actor == "agent"]
    assert [m.kind for m in parts] == ["agent_reply"] * 3
    assert [m.meta["part"] for m in parts] == ["1/3", "2/3", "3/3"]
    assert parts[0].meta["chatwoot_message_id"] == 2412

    # 184: el link de pago sale tal cual, no como [ENLACE]
    link = conversations[184].messages[1]
    assert link.kind == "payment_link"
    assert "https://pay.hotmart.com/F106691755G?off=bxjge6zq" in link.text
    assert conversations[184].apparent_objective == "Pedido del enlace de pago"
    assert conversations[186].apparent_objective == "Consulta de precio o formas de pago"

    context = conversations[186].context
    assert context["conversation_url"] == f"{CHATWOOT_URL}/app/accounts/1/conversations/186"
    assert context["contact"] == {
        "id": 9186,
        "name": "Gustavo Ejemplo",
        "phone": "+5730000000186",
        "email": "gustavo.ejemplo@example.test",
    }
    assert context["conversation"]["labels"] == ["automation_paused"]
    assert context["conversation"]["status"] == "open"
    assert context["conversation"]["created_at"] == _iso(1790438176)
    assert conversations[177].context["conversation"]["can_reply"] is False
    assert conversations[177].context["conversation"]["first_reply_at"] == _iso(1790281926)
    assert "Automatización pausada" in conversations[186].observed_outcome


def test_v2_admits_conversations_the_agent_never_answered() -> None:
    package = _collect_v2()
    # 177 no tiene ninguna respuesta del agente en la ventana (solo la plantilla de
    # reactivacion, que es del monitor, y el equipo). En v1 ni siquiera entraria.
    conversation = _by_conversation(package)[177]
    assert any(m.kind == "reactivation_template" for m in conversation.messages)
    assert not any(m.kind == "agent_reply" for m in conversation.messages)
    assert "Atendida por el equipo" in conversation.observed_outcome


def test_v1_collector_is_untouched_by_the_v2_flag() -> None:
    package = _collector(package_version=1).collect(
        tenant_ref="lancemos",
        scope_ref="psicologajohanna-agent-bot-19",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert package.schema_version == PACKAGE_SCHEMA_V1
    combined = " ".join(m.text for c in package.conversations for m in c.messages)
    assert "https://" not in combined
    assert "[ENLACE]" in combined
    assert "Arturo" not in combined
    assert all(c.context == {} for c in package.conversations)
    assert {m.actor for c in package.conversations for m in c.messages} == {"prospect", "agent"}


def test_v2_sanitizer_keeps_identity_but_redacts_secrets_and_control() -> None:
    raw = (
        "Hola, soy Catalina. Mi teléfono es +54 11 5555 2222, mi mail cat@example.com,\r\n"
        "el enlace https://pay.hotmart.com/X?off=1 y esto no: bearer abcdefghijklmnop123456 "
        "javascript:alert(1) ‮\n\n\n\nfin"
    )
    cleaned = minimize_review_text_v2(raw)
    assert "+54 11 5555 2222" in cleaned
    assert "cat@example.com" in cleaned
    assert "https://pay.hotmart.com/X?off=1" in cleaned
    assert "Catalina" in cleaned
    assert "[SECRETO REDACTADO]" in cleaned
    assert "[ESQUEMA BLOQUEADO]" in cleaned
    assert "[CONTROL]" in cleaned
    assert "‮" not in cleaned and "\r" not in cleaned
    assert "\n\n\n" not in cleaned
    assert minimize_review_text_v2(cleaned) == cleaned
    # el sanitizador v1 sigue redactando todo
    assert "[TELÉFONO]" in sanitize_review_text(raw, names=("Catalina",))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"message_type": 2, "content": "Conversation was marked resolved by Mariana Marín", "sender": None}, ("system", "conversation_resolved")),
        ({"message_type": 2, "content": "Unassigned by Bridge Service", "sender": None}, ("system", "unassigned")),
        ({"message_type": 2, "content": "Bridge Service added vip", "sender": None}, ("system", "label_added")),
        ({"message_type": 2, "content": "Something else happened", "sender": None}, ("system", "activity")),
        ({"message_type": 1, "private": False, "content": "Hola", "sender": {"type": "user", "id": 4, "name": "Mariana"}}, ("team", "team_message")),
        ({"message_type": 1, "private": True, "content": "nota", "sender": {"type": "user", "id": 4, "name": "Mariana"}}, ("team", "private_note")),
        ({"message_type": 1, "private": False, "content": "x", "content_attributes": {"recovery_first_touch_hash": "h"}, "sender": {"type": "agent_bot", "id": 1}}, ("agent", "first_touch_template")),
        ({"message_type": 1, "private": False, "content": "x", "content_attributes": {"recovery_followup_hash": "h"}, "sender": {"type": "agent_bot", "id": 1}}, ("agent", "followup_template")),
        ({"message_type": 1, "private": False, "content": "x", "content_attributes": {}, "sender": {"type": "agent_bot", "id": 1}}, ("agent", "agent_message")),
        ({"message_type": 3, "private": False, "content": "plantilla", "sender": {"type": "agent_bot", "id": 1}}, ("agent", "template_message")),
    ],
)
def test_classify_covers_every_chatwoot_shape(message: dict[str, object], expected: tuple[str, str]) -> None:
    classified = classify_chatwoot_message(message, agent_bot_id=1)
    assert classified is not None
    assert (classified.actor, classified.kind) == expected


def test_classify_ignores_other_bots_and_keeps_the_agent_decision() -> None:
    assert classify_chatwoot_message(
        {"message_type": 1, "private": False, "content": "x", "sender": {"type": "agent_bot", "id": 7}},
        agent_bot_id=1,
    ) is None
    classified = classify_chatwoot_message(
        {
            "id": 77,
            "message_type": 1,
            "private": False,
            "status": "read",
            "content": "Te derivo con el equipo.",
            "content_attributes": {
                "appointment_setter_reply_hash": "h",
                "appointment_setter_decision": "handoff",
                "appointment_setter_reason_code": "commercial_exception",
            },
            "sender": {"type": "agent_bot", "id": 1},
        },
        agent_bot_id=1,
    )
    assert classified is not None
    assert classified.kind == "agent_reply"
    assert classified.meta == {
        "chatwoot_message_id": 77,
        "decision": "handoff",
        "reason_code": "commercial_exception",
    }
    # un marcador con forma rara no entra (no rompe)
    odd = classify_chatwoot_message(
        {"message_type": 1, "private": False, "content": "x",
         "content_attributes": {"appointment_setter_reply_hash": "h", "appointment_setter_decision": "Bad Value"},
         "sender": {"type": "agent_bot", "id": 1}},
        agent_bot_id=1,
    )
    assert odd is not None and "decision" not in odd.meta


def _supabase_contexts() -> dict[str, object]:
    return {
        "186": {
            "handoffs": [
                {"occurred_at": "2026-09-26T17:03:15Z", "primary_reason_code": "commercial_exception",
                 "detail_reason_code": "explicit_human_request", "requested_by": "agent", "status": "projected"},
            ],
            "reactivations": [],
            "resumes": [],
            "opt_outs": [],
            "payment_links": [],
            "prior_reviews": [
                {"local_date": "2026-09-25", "decision": "correct_with_feedback",
                 "verbatim_feedback": "No repetir el precio", "decided_at": "2026-09-25T23:10:00Z"},
            ],
        },
        "184": {
            "handoffs": [],
            "reactivations": [],
            "resumes": [],
            "opt_outs": [],
            "payment_links": [
                {"occurred_at": "2026-09-26T02:44:02Z", "status": "purchase_matched", "source_kind": "inbound_request",
                 "chatwoot_message_id": 2391, "sck_value": "hermes|v1|01M3DSJ9CR4TQ5VMJNJMKA41TJ", "original_sck": None,
                 "checkout_url_final": "https://pay.hotmart.com/F106691755G?off=bxjge6zq", "purchased_at": "2026-09-26T03:10:00Z",
                 "offer_code": "bxjge6zq", "landing_ref": "libre-de-ansiedad"},
            ],
            "prior_reviews": [],
        },
        "177": {
            "handoffs": [],
            "reactivations": [
                {"occurred_at": "2026-09-25T20:36:59Z", "status": "sent", "template_name": "johanna_reactivacion_01",
                 "reason_code": "outside_service_window", "provider_message_id": 2375, "quiet_seconds": 28800, "failure_reason": None},
            ],
            "resumes": [{"occurred_at": "2026-09-26T13:38:00Z", "reason_code": "inbound_after_quiet_period", "quiet_seconds": 61000}],
            "opt_outs": [],
            "payment_links": [],
            "prior_reviews": [],
        },
    }


def test_apply_conversation_context_merges_supabase_rows_and_recomputes_outcome() -> None:
    package = apply_conversation_context(_collect_v2(), _supabase_contexts())
    conversations = _by_conversation(package)

    link = conversations[184].messages[1]
    assert link.kind == "payment_link"
    assert link.meta["attribution"] == "marker_only"
    assert link.meta["purchased"] is True
    assert link.meta["offer_code"] == "bxjge6zq"
    assert conversations[184].observed_outcome.startswith("Compra registrada por el link enviado")
    assert conversations[184].context["summary"]["purchase_recorded"] is True

    events_186 = conversations[186].context["events"]
    assert events_186[0]["kind"] == "handoff"
    assert events_186[0]["detail_reason_code"] == "explicit_human_request"
    assert "Derivado a humano (explicit_human_request)" in conversations[186].observed_outcome
    assert conversations[186].context["prior_reviews"][0]["decision"] == "correct_with_feedback"

    kinds_177 = [event["kind"] for event in conversations[177].context["events"]]
    assert kinds_177 == ["reactivation", "resume"]
    assert "Reactivación enviada ×1" in conversations[177].observed_outcome
    assert conversations[177].context["origin"] == "inbound"


def test_derivations_prefer_agent_decision_over_prospect_words() -> None:
    at = datetime(2026, 9, 26, 12, tzinfo=UTC)
    messages = (
        ReviewMessage("m1", "prospect", "Hola, quiero más info", at, kind="prospect_message"),
        ReviewMessage("m2", "agent", "Te mando el enlace", at, kind="agent_reply", meta={"decision": "send_payment_link"}),
    )
    assert derive_apparent_objective(messages, {}) == "Pedido del enlace de pago"
    assert derive_observed_outcome(messages, {}) == "Esperando respuesta del prospecto"
    only_prospect = (ReviewMessage("m1", "prospect", "No he podido entrar al curso", at, kind="prospect_message"),)
    assert derive_apparent_objective(only_prospect, {}) == "Soporte post-compra (acceso al programa)"
    assert derive_observed_outcome(only_prospect, {}) == "Sin respuesta del agente"


def _claim_v2() -> dict[str, object]:
    return {
        "tenant_ref": "lancemos",
        "scope_ref": "psicologajohanna-agent-bot-19",
        "window_start": "2026-09-24T00:00:00Z",
        "window_end": "2026-09-27T00:00:00Z",
        "sanitizer_version": SANITIZER_VERSION_V2,
        "selection_version": SELECTION_VERSION_V2,
    }


def test_package_items_v2_emits_context_and_six_key_messages() -> None:
    package = apply_conversation_context(_collect_v2(), _supabase_contexts())
    items, lineage = _package_items(package, _claim_v2())

    assert lineage == "release_lineage_unavailable"
    assert sorted(items[0]) == [
        "apparent_objective",
        "context",
        "conversation_ref",
        "display_label",
        "messages",
        "observed_outcome",
        "release_id",
        "release_version",
    ]
    assert all(sorted(m) == ["actor", "kind", "meta", "occurred_at", "status", "text"] for item in items for m in item["messages"])
    assert items[0]["context"]["chatwoot_conversation_id"] == 177
    assert items[0]["context"]["events"][0]["kind"] == "reactivation"
    assert json.dumps(items, ensure_ascii=False)  # serializable tal cual va a Supabase


def test_package_items_v2_rejects_context_with_unknown_keys_or_insecure_url() -> None:
    package = _collect_v2()
    first = package.conversations[0]
    tampered = replace(package, conversations=(replace(first, context={**first.context, "secret": "x"}),) + package.conversations[1:])
    with pytest.raises(RuntimeError, match="daily_feedback_context_shape_invalid"):
        _package_items(tampered, _claim_v2())
    insecure = replace(package, conversations=(replace(first, context={**first.context, "conversation_url": "http://x"}),) + package.conversations[1:])
    with pytest.raises(RuntimeError, match="daily_feedback_context_shape_invalid"):
        _package_items(insecure, _claim_v2())



def _supabase_provenance() -> dict[str, object]:
    """Lo que devuelve `get_agent_turn_provenance_v1` para la ventana.

    Solo la 186 trae release: las otras dos quedan con el marcador de "no se
    sabe", que es el estado real de toda conversacion anterior a la primera
    corrida del registrador del perfil.
    """
    return {
        "186": {
            "occurred_at": "2026-09-26T14:07:00Z",
            "release_digest": "c" * 64,
            "release_ordinal": 3,
            "model_requested": "agente-comercial",
            "model_answered": "glm-5.2",
            "bridge_release": "246de1ba",
            "context_builder_version": "shadow-context-v1",
            "context_digest": "d" * 64,
            "context_added": {"message_count": 4, "human_handoff_confirmed": False},
            "outcome": "completed",
            "confidence": "verified",
        }
    }


class V2Repository(WorkflowRepository):
    def __init__(self) -> None:
        super().__init__()
        self.context_request: dict[str, object] | None = None
        self.provenance_request: dict[str, object] | None = None

    async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        if name == "get_daily_feedback_conversation_context_v1":
            self.calls.append((name, payload))
            self.context_request = payload
            return _supabase_contexts()
        if name == "get_agent_turn_provenance_v1":
            self.calls.append((name, payload))
            self.provenance_request = payload
            return _supabase_provenance()
        result = await super().rpc(name, payload)
        if name == "claim_daily_feedback_collection_v1":
            result = {
                **result,
                "chatwoot_inbox_id": 9,
                "chatwoot_agent_bot_id": 1,
                "window_start": "2026-09-24T00:00:00Z",
                "window_end": "2026-09-27T00:00:00Z",
                "sanitizer_version": SANITIZER_VERSION_V2,
                "selection_version": SELECTION_VERSION_V2,
                "renderer_version": RENDERER_VERSION_V2,
            }
        return result


def test_scheduler_reads_conversation_context_between_collection_and_commit() -> None:
    repository = V2Repository()
    producer = FakeProducer()
    scheduler = DailyFeedbackScheduler(
        repository=repository,
        collector=_collector(),
        producer=producer,
        settings=DailyFeedbackSchedulerSettings(
            worker_id="daily-feedback-worker-1",
            tenant_ref="lancemos",
            scope_ref="psicologajohanna-agent-bot-19",
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_agent_bot_id=1,
            deletion_owner="juan",
        ),
        clock=Clock(datetime(2026, 9, 26, 23, 0, tzinfo=UTC)),
    )

    result = asyncio.run(scheduler.run_once(force_collection=True))

    assert result == {"collected": True, "notified": True, "purged": 0}
    assert [name for name, _ in repository.calls] == [
        "purge_expired_daily_feedback_v2",
        "claim_daily_feedback_collection_v1",
        "get_daily_feedback_conversation_context_v1",
        # Desde 20260928000100 la procedencia del prompt se pide en el mismo
        # enriquecido, entre el contexto y el commit.
        "get_agent_turn_provenance_v1",
        "commit_daily_feedback_batch_v1",
        "claim_daily_feedback_notification_v1",
        "mark_daily_feedback_notification_started_v1",
        "complete_daily_feedback_notification_v1",
    ]
    assert repository.context_request == {
        "p_tenant_ref": "lancemos",
        "p_scope_ref": "psicologajohanna-agent-bot-19",
        "p_chatwoot_account_id": 1,
        "p_chatwoot_inbox_id": 9,
        "p_conversation_ids": [177, 184, 186],
    }
    committed = repository.calls[4][1]["p_items"]
    assert committed[2]["context"]["chatwoot_conversation_id"] == 186
    assert committed[2]["context"]["events"][0]["kind"] == "handoff"
    assert committed[1]["messages"][1]["kind"] == "payment_link"

    # La procedencia del prompt llega al lote: hasta el 2026-09-28 estos dos
    # campos salian siempre como 'release_lineage_unavailable' y 0.
    assert repository.provenance_request == {
        "p_tenant_ref": "lancemos",
        "p_scope_ref": "psicologajohanna-agent-bot-19",
        "p_conversation_ids": [177, 184, 186],
        "p_window_start": "2026-09-24T00:00:00Z",
        "p_window_end": "2026-09-27T00:00:00Z",
    }
    assert committed[2]["release_id"] == "c" * 64
    assert committed[2]["release_version"] == 3
    assert committed[2]["context"]["agent_release"]["model_answered"] == "glm-5.2"
    assert committed[2]["context"]["agent_release"]["confidence"] == "verified"
    # Las que no tienen turno registrado se quedan con el marcador, no con el
    # release de otra conversacion.
    assert committed[0]["release_id"] == "release_lineage_unavailable"
    assert committed[0]["release_version"] == 0
    assert "agent_release" not in committed[0]["context"]
    assert committed[1]["messages"][1]["meta"]["purchased"] is True
    assert producer.commands[0].count == 1


def test_scheduler_fails_closed_when_the_context_rpc_fails() -> None:
    class BrokenContextRepository(V2Repository):
        async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]:
            if name == "get_daily_feedback_conversation_context_v1":
                self.calls.append((name, payload))
                raise RuntimeError("supabase down")
            return await super().rpc(name, payload)

    repository = BrokenContextRepository()
    scheduler = DailyFeedbackScheduler(
        repository=repository,
        collector=_collector(),
        producer=FakeProducer(),
        settings=DailyFeedbackSchedulerSettings(
            worker_id="daily-feedback-worker-1",
            tenant_ref="lancemos",
            scope_ref="psicologajohanna-agent-bot-19",
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_agent_bot_id=1,
            deletion_owner="juan",
        ),
        clock=Clock(datetime(2026, 9, 26, 23, 0, tzinfo=UTC)),
    )
    with pytest.raises(RuntimeError, match="supabase down"):
        asyncio.run(scheduler._collect_one(now=datetime(2026, 9, 26, 23, 0, tzinfo=UTC), force=True))
    assert [name for name, _ in repository.calls] == [
        "claim_daily_feedback_collection_v1",
        "get_daily_feedback_conversation_context_v1",
        "fail_daily_feedback_collection_v1",
    ]


def test_review_page_renders_lead_card_thread_kinds_and_context_lists() -> None:
    package = apply_conversation_context(_collect_v2(), _supabase_contexts())
    items, _ = _package_items(package, _claim_v2())
    item_177 = items[0]
    item_177["messages"][0]["text"] = "<script>alert(1)</script> hola"
    item_177["context"]["contact"]["name"] = "Pablo <b>Prueba</b>"
    page = {
        "status": "item",
        "local_date": "2026-09-26",
        "item_count": 3,
        "decided_count": 0,
        "item": {**item_177, "item_id": "33333333-3333-4333-8333-333333333333", "position": 1},
    }

    rendered = _review_page(
        page,
        public_ref="22222222-2222-4222-8222-222222222222",
        csrf_token="csrf",
        command_id="44444444-4444-4444-8444-444444444444",
        display_timezone="America/Bogota",
    )

    assert "Pablo &lt;b&gt;Prueba&lt;/b&gt;" in rendered
    assert "<b>Prueba</b>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "+5210000000177" in rendered
    assert f'href="{CHATWOOT_URL}/app/accounts/1/conversations/177"' in rendered
    assert 'target="_blank" rel="noopener noreferrer"' in rendered
    assert "Abrir en Chatwoot #177" in rendered
    assert "Plantilla · reactivación" in rendered
    assert "enviada por el monitor de conversaciones sin respuesta" in rendered
    assert "Nota de derivación · Bridge Service" in rendered
    assert "Automatización pausada · por Bridge Service" in rendered
    assert "Asignada · a johanna - revisión humana · por Bridge Service" in rendered
    assert "Equipo · Mariana Equipo" in rendered
    assert "horas en America/Bogota" in rendered
    # el primer contacto se muestra con fecha y en la hora de Bogota
    assert _bogota(1790279450) in rendered
    assert "después" in rendered  # el salto de un dia entre la derivacion y la reactivacion
    assert "Eventos internos" in rendered and "Reactivación · johanna_reactivacion_01 · enviado" in rendered
    assert "Reanudación · inbound_after_quiet_period" in rendered
    assert "automation_paused</span>" in rendered
    assert 'data-decision-form' in rendered and 'value="correct"' in rendered


def test_review_page_shows_decision_attribution_and_purchase_on_agent_messages() -> None:
    package = apply_conversation_context(_collect_v2(), _supabase_contexts())
    items, _ = _package_items(package, _claim_v2())
    item_184 = items[1]
    item_184["messages"][1]["meta"]["decision"] = "send_payment_link"
    item_184["messages"][1]["meta"]["reason_code"] = "payment_link_requested"
    page = {
        "status": "item", "local_date": "2026-09-26", "item_count": 3, "decided_count": 1,
        "item": {**item_184, "item_id": "33333333-3333-4333-8333-333333333333", "position": 2},
    }
    rendered = _review_page(page, public_ref="22222222-2222-4222-8222-222222222222", csrf_token="c", command_id="4", display_timezone="UTC")
    assert "Link de pago" in rendered
    assert "decisión: enviar el link de pago (payment_link_requested)" in rendered
    assert "atribución: marker_only" in rendered
    assert "compra: sí" in rendered
    assert 'class="chip chip--ok">compró</span>' in rendered
    assert "https://pay.hotmart.com/F106691755G?off=bxjge6zq" in rendered
    assert "Links de pago" in rendered and "oferta: bxjge6zq" in rendered


def test_review_page_still_renders_a_v1_item_without_context() -> None:
    page = {
        "status": "item", "local_date": "2026-09-10", "item_count": 2, "decided_count": 0,
        "item": {
            "item_id": "33333333-3333-4333-8333-333333333333", "position": 1,
            "display_label": "Conversación 01", "apparent_objective": "Revisar", "observed_outcome": "Esperando",
            "release_id": "release_lineage_unavailable", "release_version": 0,
            "messages": [
                {"actor": "prospect", "occurred_at": "2026-09-10T12:00:00Z", "text": "¿Qué incluye?"},
                {"actor": "agent", "occurred_at": "2026-09-10T12:01:00Z", "text": "Te explico."},
            ],
        },
    }
    rendered = _review_page(page, public_ref="22222222-2222-4222-8222-222222222222", csrf_token="c", command_id="4")
    assert 'class="lead-card"' not in rendered
    assert "Lead · 12:00" in rendered
    assert "Agente · 12:01" in rendered


def test_runtime_settings_declare_v2_versions_and_reviewer_timezone() -> None:
    environment = {
        "DAILY_FEEDBACK_PUBLIC_ORIGIN": "https://reviews.example.test",
        "SUPABASE_BASE_URL": "https://project.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
        "SLACK_OIDC_CLIENT_ID": "client-id",
        "SLACK_OIDC_CLIENT_SECRET": "client-secret",
        "SLACK_OIDC_TEAM_ID": "T12345678",
        "DAILY_FEEDBACK_REVIEWERS_JSON": json.dumps([
            {"reviewer_ref": "dan-schwab", "slack_user_id": "U12345678", "deletion_accountable": True},
            {"reviewer_ref": "mariana-marin", "slack_user_id": "U87654321", "deletion_accountable": True},
            {"reviewer_ref": "juan-martitegui", "slack_user_id": "U11111111", "deletion_accountable": True},
            {"reviewer_ref": "marcela-pineda", "slack_user_id": "U22222222", "deletion_accountable": True},
        ]),
        "DAILY_FEEDBACK_MANUAL_RUN_TOKEN": "manual-run-token-with-more-than-32-chars",
        "DAILY_FEEDBACK_WORKER_ID": "daily-feedback-worker-1",
        "SLACK_CONNECTOR_BASE_URL": "https://connector.example.test",
        "SLACK_CONNECTOR_BEARER_TOKEN": "connector-token-with-more-than-32-chars",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_API_ACCESS_TOKEN": "chatwoot-secret",
        "DAILY_FEEDBACK_PSEUDONYMIZATION_KEY": "pseudonym-key-with-more-than-32-characters",
        "DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": "https://supabase.com/docs/guides/platform/security",
        "DAILY_FEEDBACK_RETENTION_HOURS": "72",
        "DAILY_FEEDBACK_DELETION_POLICY_REF": "johanna-joint-reviewer-accountability-v1",
        "DAILY_FEEDBACK_TIMEZONE": "America/Bogota",
        "DAILY_FEEDBACK_DAILY_AT": "18:00:00",
        "DAILY_FEEDBACK_TENANT_REF": "lancemos",
        "DAILY_FEEDBACK_SCOPE_REF": "psicologajohanna-agent-bot-19",
        "DAILY_FEEDBACK_SLACK_TENANT_REF": "johanna",
    }
    settings = DailyFeedbackRuntimeSettings.from_env(environment)
    assert settings.application.sanitizer_version == SANITIZER_VERSION_V2
    assert settings.application.selection_version == SELECTION_VERSION_V2
    assert settings.application.renderer_version == RENDERER_VERSION_V2
    assert settings.application.configure_payload()["p_sanitizer_version"] == "identity-preserving-redaction-v2"
    assert "get_daily_feedback_conversation_context_v1" in SupabaseDailyFeedbackRepository._ALLOWED_RPCS
    with pytest.raises(ValueError, match="invalid_daily_feedback_display_timezone"):
        DailyFeedbackWebSettings(
            public_origin="https://reviews.example.test",
            session_hmac_key=b"k" * 32,
            slack_team_id="T12345678",
            display_timezone="Marte/Olympus",
        )


def test_bridge_stamps_agent_decision_and_reason_on_the_published_reply(tmp_path: Path) -> None:
    posted: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"payload": [{
                "id": 10, "message_type": 0, "private": False, "content": "Hola",
                "sender": {"type": "contact", "id": 20},
            }]})
        assert request.method == "POST"
        body = json.loads(request.content)
        posted.append(body)
        return httpx.Response(200, json={
            "id": 11, "conversation_id": 2, "message_type": 1, "private": False,
            "content": body["content"], "content_attributes": body["content_attributes"],
            "sender": {"type": "agent_bot", "id": 1},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(httpx.MockTransport(handler)),
    )

    result = asyncio.run(client.send_agent_bot_reply(
        conversation_id=2,
        trigger_message_id=10,
        delivery_id="decision-marker",
        content="Te derivo con el equipo.",
        agent_decision="handoff",
        agent_reason_code="commercial_exception",
    ))

    assert result == {"status": "sent", "message_id": 11}
    attributes = posted[0]["content_attributes"]
    assert attributes["appointment_setter_decision"] == "handoff"
    assert attributes["appointment_setter_reason_code"] == "commercial_exception"
    assert "appointment_setter_reply_hash" in attributes

    with pytest.raises(ChatwootProtocolError, match="invalid_agent_decision_marker"):
        asyncio.run(client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=12,
            delivery_id="decision-marker-bad",
            content="x",
            agent_decision="Bad Value",
        ))
