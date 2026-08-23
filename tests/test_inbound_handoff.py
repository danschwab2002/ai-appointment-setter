import asyncio
from types import SimpleNamespace

from bridge.inbound_handoff import request_handoff_for_inbound_proposal
from bridge.supabase import InboundCommercialCaseAdmissionResult


def _admission(*, outcome: str = "created") -> InboundCommercialCaseAdmissionResult:
    return InboundCommercialCaseAdmissionResult(
        outcome=outcome,
        commercial_case_id="case-1",
        contact_id="contact-1",
        channel_identity_id="identity-1",
        conversation_id="conversation-1",
        automation_status="draft_only",
    )


def test_handoff_proposal_requests_one_deterministic_inbound_handoff() -> None:
    calls: list[dict[str, object]] = []

    class SupabaseStub:
        async def request_inbound_human_handoff(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(outcome="requested", handoff_request_id="handoff-1")

    result = asyncio.run(
        request_handoff_for_inbound_proposal(
            proposal={"decision": "handoff", "reason_code": "anything_model_supplied"},
            admission=_admission(),
            external_conversation_id=123,
            trigger_message_id=789,
            projection_policy_key="lancemos-inbound-handoff",
            projection_policy_version=1,
            supabase=SupabaseStub(),
            now="2026-08-23T22:00:00+00:00",
        )
    )

    assert result is not None
    assert calls == [
        {
            "commercial_case_id": "case-1",
            "command_key": "handoff:inbound:db10a6a43449a5925958d870b55db69cd18a6887dc389884f09bfaed2f36369f",
            "reason_code": "commercial_exception",
            "projection_policy_key": "lancemos-inbound-handoff",
            "projection_policy_version": 1,
            "now": "2026-08-23T22:00:00+00:00",
        }
    ]


def test_non_handoff_proposal_never_calls_durable_handoff() -> None:
    class SupabaseStub:
        async def request_inbound_human_handoff(self, **_: object) -> object:
            raise AssertionError("non-handoff proposal must remain side-effect free")

    result = asyncio.run(
        request_handoff_for_inbound_proposal(
            proposal={"decision": "ask_question", "reason_code": "missing_context"},
            admission=_admission(),
            external_conversation_id=123,
            trigger_message_id=789,
            projection_policy_key="lancemos-inbound-handoff",
            projection_policy_version=1,
            supabase=SupabaseStub(),
            now="2026-08-23T22:00:00+00:00",
        )
    )

    assert result is None


def test_conflicted_admission_cannot_request_handoff() -> None:
    class SupabaseStub:
        async def request_inbound_human_handoff(self, **_: object) -> object:
            raise AssertionError("conflicted admission must remain side-effect free")

    result = asyncio.run(
        request_handoff_for_inbound_proposal(
            proposal={"decision": "handoff", "reason_code": "commercial_exception"},
            admission=_admission(outcome="evidence_conflict"),
            external_conversation_id=123,
            trigger_message_id=789,
            projection_policy_key="lancemos-inbound-handoff",
            projection_policy_version=1,
            supabase=SupabaseStub(),
            now="2026-08-23T22:00:00+00:00",
        )
    )

    assert result is None
