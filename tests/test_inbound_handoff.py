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
            "detail_reason_code": "anything_model_supplied",
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


def test_payment_link_reason_reaches_supabase_as_the_detail_code() -> None:
    """El motivo medido en produccion el 2026-09-24 sobre la conversacion 158.

    El agente decidio send_payment_link, deliver_checkout_issuance_v2 devolvio
    blocked porque el contacto ya habia comprado, y el worker compuso
    payment_link_purchase_already_approved. Ese motivo tiene que llegar a la
    fila: primary_reason_code sigue siendo la taxonomia cerrada.
    """
    calls: list[dict[str, object]] = []

    class SupabaseStub:
        async def request_inbound_human_handoff(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(outcome="requested", handoff_request_id="handoff-1")

    result = asyncio.run(
        request_handoff_for_inbound_proposal(
            proposal={
                "decision": "handoff",
                "qualification_status": "needs_human",
                "reason_code": "payment_link_purchase_already_approved",
            },
            admission=_admission(),
            external_conversation_id=158,
            trigger_message_id=2233,
            projection_policy_key="lancemos-inbound-handoff",
            projection_policy_version=1,
            supabase=SupabaseStub(),
            now="2026-09-24T13:03:34+00:00",
        )
    )

    assert result is not None
    assert calls[0]["reason_code"] == "commercial_exception"
    assert calls[0]["detail_reason_code"] == (
        "payment_link_purchase_already_approved"
    )


def test_unusable_reason_code_never_blocks_the_handoff() -> None:
    """Perder el motivo es malo; perder la derivacion deja a alguien esperando."""
    calls: list[dict[str, object]] = []

    class SupabaseStub:
        async def request_inbound_human_handoff(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(outcome="requested", handoff_request_id="handoff-1")

    for proposal in (
        {"decision": "handoff"},
        {"decision": "handoff", "reason_code": None},
        {"decision": "handoff", "reason_code": ""},
        {"decision": "handoff", "reason_code": "Payment_Link_Blocked"},
        {"decision": "handoff", "reason_code": "payment link blocked"},
        {"decision": "handoff", "reason_code": "9_leading_digit"},
        {"decision": "handoff", "reason_code": "x" * 101},
        {"decision": "handoff", "reason_code": 1234},
    ):
        calls.clear()
        result = asyncio.run(
            request_handoff_for_inbound_proposal(
                proposal=proposal,
                admission=_admission(),
                external_conversation_id=123,
                trigger_message_id=789,
                projection_policy_key="lancemos-inbound-handoff",
                projection_policy_version=1,
                supabase=SupabaseStub(),
                now="2026-08-23T22:00:00+00:00",
            )
        )
        assert result is not None, proposal
        assert calls[0]["detail_reason_code"] is None, proposal
        assert calls[0]["reason_code"] == "commercial_exception", proposal


def test_longest_accepted_reason_code_still_reaches_the_row() -> None:
    calls: list[dict[str, object]] = []

    class SupabaseStub:
        async def request_inbound_human_handoff(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(outcome="requested", handoff_request_id="handoff-1")

    longest = "p" + "a" * 99
    asyncio.run(
        request_handoff_for_inbound_proposal(
            proposal={"decision": "handoff", "reason_code": longest},
            admission=_admission(),
            external_conversation_id=123,
            trigger_message_id=789,
            projection_policy_key="lancemos-inbound-handoff",
            projection_policy_version=1,
            supabase=SupabaseStub(),
            now="2026-08-23T22:00:00+00:00",
        )
    )

    assert len(longest) == 100
    assert calls[0]["detail_reason_code"] == longest
