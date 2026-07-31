"""Tests for the recovery agent client and proposal validation."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from bridge.recovery_agent import RecoveryAgentClient, is_valid_recovery_proposal


# ── Proposal validation ─────────────────────────────────────────────


def _valid_proposal() -> dict[str, object]:
    return {
        "action": "send_first_touch",
        "reason_code": "first_touch",
        "message": "¡Hola! Soy el asistente virtual de Dan. Vi que estabas mirando el curso. ¿Te quedó alguna duda?",
        "lead_stage": "new",
        "current_goal": "iniciar conversación de recupero",
    }


def test_accepts_valid_send_first_touch_proposal() -> None:
    assert is_valid_recovery_proposal(_valid_proposal()) is True


def test_accepts_valid_abort_proposal() -> None:
    proposal = _valid_proposal()
    proposal["action"] = "abort"
    proposal["reason_code"] = "contact_blocked"
    proposal["message"] = None
    proposal["current_goal"] = "no contactar, contacto bloqueado"
    assert is_valid_recovery_proposal(proposal) is True


def test_accepts_valid_hold_proposal() -> None:
    proposal = _valid_proposal()
    proposal["action"] = "hold"
    proposal["reason_code"] = "recovery_case_active"
    proposal["message"] = None
    proposal["current_goal"] = "esperar a que el caso existente avance"
    assert is_valid_recovery_proposal(proposal) is True


def test_accepts_valid_handoff_proposal() -> None:
    proposal = _valid_proposal()
    proposal["action"] = "handoff"
    proposal["reason_code"] = "stale_conversation_review"
    proposal["message"] = None
    proposal["current_goal"] = "derivar a humano por conversación antigua"
    assert is_valid_recovery_proposal(proposal) is True


def test_rejects_message_on_non_send_action() -> None:
    proposal = _valid_proposal()
    proposal["action"] = "hold"
    proposal["reason_code"] = "recovery_case_active"
    # message should be null for hold
    proposal["message"] = "no debería estar acá"
    assert is_valid_recovery_proposal(proposal) is False


def test_rejects_empty_message_on_send_first_touch() -> None:
    proposal = _valid_proposal()
    proposal["message"] = "   "
    assert is_valid_recovery_proposal(proposal) is False


def test_rejects_message_over_500_chars() -> None:
    proposal = _valid_proposal()
    proposal["message"] = "x" * 501
    assert is_valid_recovery_proposal(proposal) is False


def test_rejects_invalid_action() -> None:
    proposal = _valid_proposal()
    proposal["action"] = "send_message"
    assert is_valid_recovery_proposal(proposal) is False


def test_rejects_invalid_reason_code() -> None:
    proposal = _valid_proposal()
    proposal["reason_code"] = "because_i_say_so"
    assert is_valid_recovery_proposal(proposal) is False


def test_rejects_invalid_lead_stage() -> None:
    proposal = _valid_proposal()
    proposal["lead_stage"] = "prospect"
    assert is_valid_recovery_proposal(proposal) is False


def test_rejects_missing_field() -> None:
    proposal = _valid_proposal()
    del proposal["current_goal"]
    assert is_valid_recovery_proposal(proposal) is False


def test_rejects_extra_field() -> None:
    proposal = _valid_proposal()
    proposal["extra"] = "nope"
    assert is_valid_recovery_proposal(proposal) is False


# ── RecoveryAgentClient ─────────────────────────────────────────────


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self.body = body or {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status_code,
            json=self.body,
            request=request,
        )


def _run(coro):
    return asyncio.run(coro)


def test_returns_proposal_when_agent_responds_valid() -> None:
    proposal = _valid_proposal()
    transport = _MockTransport(
        body={
            "choices": [
                {"message": {"content": json.dumps(proposal)}}
            ]
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=transport,
        )
        result = _run(client.request_proposal(
            event_id="evt-001",
            situation_report={"event_id": "evt-001", "source": "hotmart"},
        ))

    assert result is not None
    assert result["action"] == "send_first_touch"
    assert result["reason_code"] == "first_touch"
    assert len(transport.requests) == 1
    # Verify the request body includes the situation_report
    req_body = json.loads(transport.requests[0].content)
    assert "situation_report" in req_body["messages"][0]["content"]


def test_returns_none_when_agent_unavailable() -> None:
    class _ErrorTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("unavailable", request=request)

    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=_ErrorTransport(),
        )
        result = _run(client.request_proposal(
            event_id="evt-002",
            situation_report={"event_id": "evt-002"},
        ))

    assert result is None


def test_returns_none_when_agent_returns_invalid_proposal() -> None:
    transport = _MockTransport(
        body={
            "choices": [
                {"message": {"content": "this is not json"}}
            ]
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=transport,
        )
        result = _run(client.request_proposal(
            event_id="evt-003",
            situation_report={"event_id": "evt-003"},
        ))

    assert result is None


def test_persists_proposal_to_disk() -> None:
    import hashlib
    proposal = _valid_proposal()
    transport = _MockTransport(
        body={
            "choices": [
                {"message": {"content": json.dumps(proposal)}}
            ]
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        proposals_dir = Path(tmp)
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=proposals_dir,
            transport=transport,
        )
        _run(client.request_proposal(
            event_id="evt-004",
            situation_report={"event_id": "evt-004"},
        ))

        digest = hashlib.sha256(b"evt-004").hexdigest()
        result_file = proposals_dir / f"{digest}.json"
        assert result_file.exists()
        persisted = json.loads(result_file.read_text())
        assert persisted["status"] == "completed"
        assert persisted["proposal"]["action"] == "send_first_touch"
