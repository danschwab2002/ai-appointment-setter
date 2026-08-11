"""Tests for the recovery agent client and proposal validation."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from itertools import product
from pathlib import Path
from typing import cast

import httpx
import pytest

from bridge.recovery_agent import (
    _RECOVERY_DECISION_POLICY,
    FollowupMessageProposal,
    RecoveryAgentClient,
    is_valid_followup_message_proposal,
    is_valid_recovery_proposal,
    required_recovery_decision,
)
from bridge.supabase import FollowupExecutionContext


# ── Proposal validation ─────────────────────────────────────────────


def _valid_proposal() -> dict[str, object]:
    return {
        "action": "send_first_touch",
        "reason_code": "first_touch",
        "message": "¡Hola! Soy el asistente virtual de Dan. Vi que estabas mirando el curso. ¿Te quedó alguna duda?",
        "lead_stage": "new",
        "current_goal": "iniciar conversación de recupero",
    }


def _situation_report(event_id: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "source": "hotmart",
        "authoritative_context_complete": True,
        "contact_blocked": False,
        "phone_available": True,
        "any_conversation_human_takeover": False,
        "has_active_conversation": False,
        "has_open_recovery_case": False,
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


@pytest.mark.parametrize(
    ("proposal", "expected"),
    [
        (
            {
                "strategy": "abrir una conversación breve y consultiva",
                "message": "Hola, ¿te quedó alguna duda sobre el programa?",
            },
            True,
        ),
        ({"strategy": "", "message": "Hola"}, False),
        ({"strategy": "consultiva", "message": "   "}, False),
        ({"strategy": "x" * 121, "message": "Hola"}, False),
        ({"strategy": "consultiva", "message": "x" * 501}, False),
        (
            {
                "strategy": "consultiva",
                "message": "Hola",
                "action": "send_message",
            },
            False,
        ),
        (
            {
                "proposal": "suggest_handoff",
                "reason_code": "commercial_exception",
            },
            True,
        ),
        (
            {
                "proposal": "suggest_handoff",
                "reason_code": "insufficient_context",
            },
            False,
        ),
    ],
)
def test_validates_bounded_followup_message_proposal(
    proposal: dict[str, object], expected: bool
) -> None:
    assert is_valid_followup_message_proposal(proposal) is expected


# ── RecoveryAgentClient ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, {"action": "send_first_touch", "reason_code": "first_touch"}),
        (
            {"authoritative_context_complete": False},
            {"action": "handoff", "reason_code": "insufficient_context"},
        ),
        (
            {
                "contact_blocked": True,
                "phone_available": False,
                "any_conversation_human_takeover": True,
                "has_active_conversation": True,
                "has_open_recovery_case": True,
            },
            {"action": "abort", "reason_code": "contact_blocked"},
        ),
        (
            {"phone_available": False},
            {"action": "abort", "reason_code": "no_phone_available"},
        ),
        (
            {"any_conversation_human_takeover": True},
            {"action": "abort", "reason_code": "human_takeover_active"},
        ),
        (
            {"has_active_conversation": True},
            {"action": "hold", "reason_code": "active_conversation_exists"},
        ),
        (
            {"has_open_recovery_case": True},
            {"action": "hold", "reason_code": "recovery_case_active"},
        ),
    ],
)
def test_required_recovery_decision(
    overrides: dict[str, object],
    expected: dict[str, str],
) -> None:
    report: dict[str, object] = {
        "authoritative_context_complete": True,
        "contact_blocked": False,
        "phone_available": True,
        "any_conversation_human_takeover": False,
        "has_active_conversation": False,
        "has_open_recovery_case": False,
    }
    report.update(overrides)

    assert required_recovery_decision(report) == expected


def test_required_recovery_decision_handoffs_when_guard_is_missing() -> None:
    report = _situation_report("evt-missing-guard")
    del report["contact_blocked"]

    assert required_recovery_decision(report) == {
        "action": "handoff",
        "reason_code": "insufficient_context",
    }


def test_executable_decision_matches_declarative_policy_exhaustively() -> None:
    fields = cast(
        list[str], _RECOVERY_DECISION_POLICY["authoritative_fields"]
    )
    rules = cast(list[dict[str, object]], _RECOVERY_DECISION_POLICY["rules"])
    default = cast(dict[str, str], _RECOVERY_DECISION_POLICY["default"])
    missing = cast(
        dict[str, str],
        _RECOVERY_DECISION_POLICY["on_missing_authoritative_field"],
    )

    for values in product((False, True), repeat=len(fields)):
        report: dict[str, object] = dict(zip(fields, values, strict=True))
        expected = default
        for rule in rules:
            assert isinstance(rule, dict)
            condition = cast(dict[str, bool], rule["when"])
            if all(report[key] == value for key, value in condition.items()):
                expected = {
                    "action": rule["action"],
                    "reason_code": rule["reason_code"],
                }
                break
        assert required_recovery_decision(report) == expected

    complete_report: dict[str, object] = {
        field: False for field in fields
    }
    for field in fields:
        incomplete_report = complete_report.copy()
        del incomplete_report[field]
        assert required_recovery_decision(incomplete_report) == missing


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
            situation_report=_situation_report("evt-001"),
        ))

    assert result is not None
    assert result["action"] == "send_first_touch"
    assert result["reason_code"] == "first_touch"
    assert len(transport.requests) == 1
    # Verify the request body includes the situation_report
    req_body = json.loads(transport.requests[0].content)
    context = json.loads(req_body["messages"][0]["content"])
    assert "situation_report" in context
    assert context["decision_policy"]["default"] == {
        "action": "send_first_touch",
        "reason_code": "first_touch",
    }
    assert context["decision_policy"]["authoritative_fields"] == [
        "authoritative_context_complete",
        "contact_blocked",
        "phone_available",
        "any_conversation_human_takeover",
        "has_active_conversation",
        "has_open_recovery_case",
    ]
    assert context["required_decision"] == {
        "action": "send_first_touch",
        "reason_code": "first_touch",
    }


def test_requests_bounded_durable_followup_message() -> None:
    proposal = {
        "strategy": "recordatorio consultivo",
        "message": "Hola Ana, ¿te quedó alguna duda sobre Curso Uno?",
    }
    transport = _MockTransport(
        body={"choices": [{"message": {"content": json.dumps(proposal)}}]}
    )
    execution_context = FollowupExecutionContext(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
        contact_id="contact-001",
        source_event_id="event-001",
        buyer_name="Ana",
        buyer_email="ana@example.test",
        buyer_phone="15555550100",
        product_name="Curso Uno",
        offer_code="OFERTA1",
        current_goal="iniciar conversación",
        lead_stage="new",
    )

    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=transport,
        )
        result = _run(client.request_followup_message(
            attempt_id="attempt-001",
            execution_context=execution_context,
        ))

    assert result == FollowupMessageProposal(
        strategy="recordatorio consultivo",
        message="Hola Ana, ¿te quedó alguna duda sobre Curso Uno?",
    )
    request = transport.requests[0]
    assert request.headers["Idempotency-Key"] == __import__("hashlib").sha256(
        b"followup:attempt-001"
    ).hexdigest()
    request_body = json.loads(request.content)
    agent_context = json.loads(request_body["messages"][0]["content"])
    assert agent_context["execution_context"] == {
        "action_type": "first_contact_review",
        "step_key": "first_contact",
        "buyer_name": "Ana",
        "product_name": "Curso Uno",
        "offer_code": "OFERTA1",
        "current_goal": "iniciar conversación",
        "lead_stage": "new",
    }
    serialized = json.dumps(agent_context)
    assert "ana@example.test" not in serialized
    assert "15555550100" not in serialized
    assert "contact-001" not in serialized
    assert agent_context["required_output"] == {
        "one_of": [
            {
                "strategy": "non-empty string, max 120 characters",
                "message": "non-empty string, max 500 characters",
            },
            {
                "proposal": "suggest_handoff",
                "reason_code": [
                    "commercial_exception",
                    "explicit_human_request",
                    "policy_requires_human",
                ],
            },
        ]
    }


def test_parses_followup_message_wrapped_in_markdown_fences() -> None:
    # Hermes (and most chat LLMs) frequently wrap JSON in ```json fences.
    # The bounded proposal must be recovered from that envelope.
    fenced = (
        "```json\n"
        "{\n"
        '  "strategy": "Primer contacto cálido para retomar interés",\n'
        '  "message": "¡Hola Ana! Vi que estuviste viendo Curso Uno. '
        '¿Alguna duda que pueda resolverte?"\n'
        "}\n"
        "```"
    )
    transport = _MockTransport(
        body={"choices": [{"message": {"content": fenced}}]}
    )
    execution_context = FollowupExecutionContext(
        action_id="action-fence",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-fence",
        contact_id="contact-fence",
        source_event_id="event-fence",
        buyer_name="Ana",
        buyer_email="ana@example.test",
        buyer_phone="15555550100",
        product_name="Curso Uno",
        offer_code="OFERTA1",
        current_goal="iniciar conversación",
        lead_stage="new",
    )

    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=transport,
        )
        result = _run(client.request_followup_message(
            attempt_id="attempt-fence",
            execution_context=execution_context,
        ))

    assert result == FollowupMessageProposal(
        strategy="Primer contacto cálido para retomar interés",
        message=(
            "¡Hola Ana! Vi que estuviste viendo Curso Uno. "
            "¿Alguna duda que pueda resolverte?"
        ),
    )


def test_agent_json_parser_rejects_errors_and_resists_redos() -> None:
    import time

    from bridge.recovery_agent import _parse_agent_json_object

    # Fail-closed: a raw provider error string (e.g. HTTP 429) must not parse.
    assert _parse_agent_json_object(
        "API call failed after 3 retries: HTTP 429"
    ) is None
    # A non-object JSON value must not parse.
    assert _parse_agent_json_object("[1, 2, 3]") is None
    # Direct JSON, fenced JSON, and fence-without-language all parse.
    assert _parse_agent_json_object('{"strategy": "a", "message": "b"}') == {
        "strategy": "a",
        "message": "b",
    }
    assert _parse_agent_json_object(
        '```json\n{"strategy": "a", "message": "b"}\n```'
    ) == {"strategy": "a", "message": "b"}

    # Regression: an unterminated fence followed by a long whitespace run must
    # resolve in linear time (the previous regex backtracked catastrophically).
    malicious = "```json\n" + " " * 200_000
    start = time.perf_counter()
    result = _parse_agent_json_object(malicious)
    elapsed = time.perf_counter() - start
    assert result is None
    assert elapsed < 1.0, f"parser too slow ({elapsed:.2f}s) — possible ReDoS"


def test_reuses_completed_durable_followup_proposal_without_second_agent_call() -> None:
    proposal = {
        "strategy": "recordatorio consultivo",
        "message": "Hola Ana, ¿te quedó alguna duda?",
    }
    transport = _MockTransport(
        body={"choices": [{"message": {"content": json.dumps(proposal)}}]}
    )
    execution_context = FollowupExecutionContext(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
        contact_id="contact-001",
        source_event_id="event-001",
        buyer_name="Ana",
        buyer_email=None,
        buyer_phone=None,
        product_name="Curso Uno",
        offer_code=None,
        current_goal=None,
        lead_stage="new",
    )

    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=transport,
        )
        first = _run(client.request_followup_message(
            attempt_id="attempt-replay",
            execution_context=execution_context,
        ))
        replay = _run(client.request_followup_message(
            attempt_id="attempt-replay",
            execution_context=execution_context,
        ))

    assert first == replay == FollowupMessageProposal(**proposal)
    assert len(transport.requests) == 1


def test_serializes_concurrent_followup_requests_for_same_attempt() -> None:
    proposal = {
        "strategy": "recordatorio consultivo",
        "message": "Hola Ana, ¿te quedó alguna duda?",
    }

    class DelayedTransport(_MockTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            return await super().handle_async_request(request)

    transport = DelayedTransport(
        body={"choices": [{"message": {"content": json.dumps(proposal)}}]}
    )
    execution_context = FollowupExecutionContext(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
        contact_id="contact-001",
        source_event_id="event-001",
        buyer_name="Ana",
        buyer_email=None,
        buyer_phone=None,
        product_name="Curso Uno",
        offer_code=None,
        current_goal=None,
        lead_stage="new",
    )

    async def exercise(
        client: RecoveryAgentClient,
    ) -> tuple[FollowupMessageProposal | None, FollowupMessageProposal | None]:
        return await asyncio.gather(
            client.request_followup_message(
                attempt_id="attempt-concurrent",
                execution_context=execution_context,
            ),
            client.request_followup_message(
                attempt_id="attempt-concurrent",
                execution_context=execution_context,
            ),
        )

    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=transport,
        )
        results = _run(exercise(client))

    assert results == [FollowupMessageProposal(**proposal)] * 2
    assert len(transport.requests) == 1


def test_repeated_cancellation_waits_for_pending_file_lock() -> None:
    proposal = {"strategy": "consultiva", "message": "Hola Ana"}

    class HeldTransport(_MockTransport):
        def __init__(self) -> None:
            super().__init__(
                body={"choices": [{"message": {"content": json.dumps(proposal)}}]}
            )
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.entered.set()
            await self.release.wait()
            return await super().handle_async_request(request)

    transport = HeldTransport()
    execution_context = FollowupExecutionContext(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
        contact_id="contact-001",
        source_event_id="event-001",
        buyer_name="Ana",
        buyer_email=None,
        buyer_phone=None,
        product_name="Curso Uno",
        offer_code=None,
        current_goal=None,
        lead_stage="new",
    )

    async def exercise(client: RecoveryAgentClient) -> None:
        holder = asyncio.create_task(client.request_followup_message(
            attempt_id="attempt-cancel",
            execution_context=execution_context,
        ))
        await transport.entered.wait()
        waiter = asyncio.create_task(client.request_followup_message(
            attempt_id="attempt-cancel",
            execution_context=execution_context,
        ))
        await asyncio.sleep(0.01)
        waiter.cancel()
        await asyncio.sleep(0.01)
        waiter.cancel()
        await asyncio.sleep(0.01)
        assert waiter.done() is False
        transport.release.set()
        assert await holder == FollowupMessageProposal(**proposal)
        with pytest.raises(asyncio.CancelledError):
            await waiter

    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=transport,
        )
        _run(exercise(client))

    assert len(transport.requests) == 1


def test_replaces_schema_invalid_completed_followup_artifact() -> None:
    valid = {
        "strategy": "consultiva",
        "message": "Hola Ana, ¿te puedo ayudar?",
    }
    transport = _MockTransport(
        body={"choices": [{"message": {"content": json.dumps(valid)}}]}
    )
    execution_context = FollowupExecutionContext(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
        contact_id="contact-001",
        source_event_id="event-001",
        buyer_name="Ana",
        buyer_email=None,
        buyer_phone=None,
        product_name="Curso Uno",
        offer_code=None,
        current_goal=None,
        lead_stage="new",
    )

    with tempfile.TemporaryDirectory() as tmp:
        proposals_dir = Path(tmp)
        digest = __import__("hashlib").sha256(
            b"followup:attempt-poisoned"
        ).hexdigest()
        poisoned_path = proposals_dir / f"{digest}.json"
        poisoned_path.write_text(json.dumps({
            "status": "completed",
            "attempt_id": "attempt-poisoned",
            "proposal": {"strategy": "", "message": "bad"},
        }))
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=proposals_dir,
            transport=transport,
        )
        result = _run(client.request_followup_message(
            attempt_id="attempt-poisoned",
            execution_context=execution_context,
        ))
        persisted = json.loads(poisoned_path.read_text())

    assert result == FollowupMessageProposal(**valid)
    assert len(transport.requests) == 1
    assert persisted == {
        "status": "completed",
        "attempt_id": "attempt-poisoned",
        "proposal": valid,
    }


def test_fsyncs_proposal_directory_after_atomic_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = {"strategy": "consultiva", "message": "Hola Ana"}
    transport = _MockTransport(
        body={"choices": [{"message": {"content": json.dumps(proposal)}}]}
    )
    execution_context = FollowupExecutionContext(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
        contact_id="contact-001",
        source_event_id="event-001",
        buyer_name="Ana",
        buyer_email=None,
        buyer_phone=None,
        product_name="Curso Uno",
        offer_code=None,
        current_goal=None,
        lead_stage="new",
    )
    fsynced_directory = False
    real_fsync = os.fsync

    def observe_fsync(fd: int) -> None:
        nonlocal fsynced_directory
        fsynced_directory = fsynced_directory or stat.S_ISDIR(os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    with tempfile.TemporaryDirectory() as tmp:
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=Path(tmp),
            transport=transport,
        )
        result = _run(client.request_followup_message(
            attempt_id="attempt-fsync",
            execution_context=execution_context,
        ))

    assert result == FollowupMessageProposal(**proposal)
    assert fsynced_directory is True


def test_valid_retry_replaces_failed_durable_followup_artifact() -> None:
    invalid = {"strategy": "", "message": "Hola"}
    valid = {"strategy": "consultiva", "message": "Hola, ¿te puedo ayudar?"}
    transport = _MockTransport(
        body={"choices": [{"message": {"content": json.dumps(invalid)}}]}
    )
    execution_context = FollowupExecutionContext(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
        contact_id="contact-001",
        source_event_id="event-001",
        buyer_name="Ana",
        buyer_email=None,
        buyer_phone=None,
        product_name="Curso Uno",
        offer_code=None,
        current_goal=None,
        lead_stage="new",
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
        first = _run(client.request_followup_message(
            attempt_id="attempt-retry",
            execution_context=execution_context,
        ))
        transport.body = {
            "choices": [{"message": {"content": json.dumps(valid)}}]
        }
        retried = _run(client.request_followup_message(
            attempt_id="attempt-retry",
            execution_context=execution_context,
        ))
        digest = __import__("hashlib").sha256(
            b"followup:attempt-retry"
        ).hexdigest()
        persisted = json.loads((proposals_dir / f"{digest}.json").read_text())

    assert first is None
    assert retried == FollowupMessageProposal(**valid)
    assert persisted == {
        "status": "completed",
        "attempt_id": "attempt-retry",
        "proposal": valid,
    }


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
            situation_report=_situation_report("evt-002"),
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
            situation_report=_situation_report("evt-003"),
        ))

    assert result is None


def test_rejects_valid_proposal_that_contradicts_required_decision() -> None:
    import hashlib

    proposal = _valid_proposal()
    transport = _MockTransport(
        body={
            "choices": [
                {"message": {"content": json.dumps(proposal)}}
            ]
        }
    )
    report = _situation_report("evt-decision-mismatch")
    report["contact_blocked"] = True

    with tempfile.TemporaryDirectory() as tmp:
        proposals_dir = Path(tmp)
        client = RecoveryAgentClient(
            base_url="https://hermes.example.test/v1",
            api_key="test-key",
            model_name="agente-comercial",
            proposals_dir=proposals_dir,
            transport=transport,
        )
        result = _run(client.request_proposal(
            event_id="evt-decision-mismatch",
            situation_report=report,
        ))

        digest = hashlib.sha256(b"evt-decision-mismatch").hexdigest()
        persisted = json.loads(
            (proposals_dir / f"{digest}.json").read_text()
        )

    assert result is None
    assert persisted["reason"] == "proposal_decision_mismatch"
    assert persisted["required_decision"] == {
        "action": "abort",
        "reason_code": "contact_blocked",
    }


def test_persists_safe_diagnostics_for_invalid_proposal() -> None:
    import hashlib

    proposal = _valid_proposal()
    private_message = "private-message-that-must-not-be-persisted"
    proposal["message"] = private_message
    proposal["unexpected"] = "private-extra-value"
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
        result = _run(client.request_proposal(
            event_id="evt-invalid-diagnostics",
            situation_report=_situation_report("evt-invalid-diagnostics"),
        ))

        digest = hashlib.sha256(b"evt-invalid-diagnostics").hexdigest()
        persisted_text = (proposals_dir / f"{digest}.json").read_text()
        persisted = json.loads(persisted_text)

    assert result is None
    assert persisted["diagnostics"] == {
        "proposal_type": "dict",
        "expected_keys_present": [
            "action",
            "current_goal",
            "lead_stage",
            "message",
            "reason_code",
        ],
        "missing_keys": [],
        "extra_key_count": 1,
        "action": "send_first_touch",
        "reason_code": "first_touch",
        "lead_stage": "new",
        "message_type": "str",
        "message_length": len(private_message),
        "current_goal_type": "str",
        "current_goal_length": len("iniciar conversación de recupero"),
        "validation_errors": ["unexpected_keys"],
    }
    assert "private-message-that-must-not-be-persisted" not in persisted_text
    assert "private-extra-value" not in persisted_text


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
            situation_report=_situation_report("evt-004"),
        ))

        digest = hashlib.sha256(b"evt-004").hexdigest()
        result_file = proposals_dir / f"{digest}.json"
        assert result_file.exists()
        persisted = json.loads(result_file.read_text())
        assert persisted["status"] == "completed"
        assert persisted["proposal"]["action"] == "send_first_touch"
