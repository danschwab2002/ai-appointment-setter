from __future__ import annotations

import hashlib
from typing import Protocol

from bridge.supabase import InboundCommercialCaseAdmissionResult


class InboundHandoffClient(Protocol):
    async def request_inbound_human_handoff(self, **kwargs: object) -> object: ...


async def request_handoff_for_inbound_proposal(
    *,
    proposal: dict[str, object],
    admission: InboundCommercialCaseAdmissionResult,
    external_conversation_id: int,
    trigger_message_id: int,
    projection_policy_key: str,
    projection_policy_version: int,
    supabase: InboundHandoffClient,
    now: str,
) -> object | None:
    if proposal.get("decision") != "handoff":
        return None
    if admission.outcome == "evidence_conflict":
        return None

    command_material = (
        f"{admission.commercial_case_id}:"
        f"{external_conversation_id}:{trigger_message_id}"
    )
    command_digest = hashlib.sha256(command_material.encode("utf-8")).hexdigest()
    return await supabase.request_inbound_human_handoff(
        commercial_case_id=admission.commercial_case_id,
        command_key=f"handoff:inbound:{command_digest}",
        reason_code="commercial_exception",
        projection_policy_key=projection_policy_key,
        projection_policy_version=projection_policy_version,
        now=now,
    )
