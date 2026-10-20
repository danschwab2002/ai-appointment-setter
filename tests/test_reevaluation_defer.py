"""La reevaluacion puede devolver ``defer`` y el bridge lo acepta.

``reevaluate_followup_action`` decide ``defer`` con ``business_window_closed``
(20260803000100, 20260805000300) cuando la accion vence con la ventana de su
politica cerrada, y la deja ella misma en ``deferred`` con ``due_at`` en la
proxima apertura. El cliente solo aceptaba execute, cancel, pause, expire y
escalate: en ATT1, del 2026-10-20 a las 03Z en adelante, cada ``defer`` tiro
``reevaluate_followup_action_invalid_row`` (unas 32 veces en 12 h, siempre
fuera de 09 a 21 de Ciudad de Mexico) y corto la vuelta entera de
``dispatch_due``: las otras acciones reclamadas en esa vuelta quedaban con su
lease hasta que vencia.

La fila es la que devuelve la funcion viva de ATT1 (``return query select
p_action_id, v_decision, v_reason, ...``), leida en solo lectura el
2026-10-20.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from bridge.supabase import (
    ChatwootAuthorityContext,
    ReevaluationDecision,
    ScheduledAction,
    SupabaseClient,
)
from bridge.worker import DurableDispatcher

NOW = "2026-10-20T13:30:00+00:00"


def _row(action_id: str, decision: str, reason: str) -> dict[str, object]:
    return {
        "action_id": action_id,
        "decision": decision,
        "reason_code": reason,
        "case_version": 3,
        "sequence_revision": 2,
    }


def _client(rows_by_action: dict[str, dict[str, object]]) -> tuple[SupabaseClient, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["p_action_id"])
        return httpx.Response(200, json=[rows_by_action[body["p_action_id"]]])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    return client, seen


def _action(action_id: str) -> ScheduledAction:
    return ScheduledAction(
        action_id=action_id,
        recovery_case_id=f"case-{action_id}",
        followup_sequence_id=f"sequence-{action_id}",
        action_type="no_reply_review",
        status="pending",
        due_at="2026-10-20T13:00:00+00:00",
        expires_at="2026-10-25T13:00:00+00:00",
        expected_case_version=3,
        policy_key="att1-carrito-tres-toques",
        policy_version=1,
        step_key="cart_second_touch",
        anchor_type="accepted_outbound_message",
        anchor_subject_internal_id=f"message-{action_id}",
        anchor_observed_at="2026-10-19T13:00:00+00:00",
        lease_owner="dispatcher-test",
        lease_generation=1,
        lease_expires_at="2026-10-20T13:35:00+00:00",
        idempotency_key=f"cart:{action_id}",
    )


def test_the_client_parses_a_defer_decision() -> None:
    client, seen = _client({"action-1": _row("action-1", "defer", "business_window_closed")})

    decision = asyncio.run(
        client.reevaluate_followup_action(
            action_id="action-1",
            worker_id="dispatcher-test",
            lease_generation=1,
            now=NOW,
            anchor_type="accepted_outbound_message",
        )
    )

    assert decision == ReevaluationDecision(
        action_id="action-1",
        decision="defer",
        reason_code="business_window_closed",
        case_version=3,
        sequence_revision=2,
    )
    assert seen == ["action-1"]


def test_a_defer_does_not_cut_the_rest_of_the_round() -> None:
    client, seen = _client(
        {
            "action-1": _row("action-1", "defer", "business_window_closed"),
            "action-2": _row("action-2", "cancel", "purchase_detected_any_offer"),
        }
    )

    class SupabaseStub:
        async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
            return [_action("action-1"), _action("action-2")]

        async def get_followup_chatwoot_context(
            self, *, action_id: str, **_: object
        ) -> ChatwootAuthorityContext:
            return ChatwootAuthorityContext(
                action_id=action_id,
                action_type="no_reply_review",
                chatwoot_account_id=None,
                external_conversation_id=None,
                expected_inbox_id=None,
                anchor_external_message_id=None,
            )

        async def reevaluate_followup_action(self, **kwargs: object) -> ReevaluationDecision:
            return await client.reevaluate_followup_action(**kwargs)  # type: ignore[arg-type]

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
    )

    decisions = asyncio.run(dispatcher.dispatch_due(now=NOW))

    assert [(d.action_id, d.decision) for d in decisions] == [
        ("action-1", "defer"),
        ("action-2", "cancel"),
    ]
    assert seen == ["action-1", "action-2"]
