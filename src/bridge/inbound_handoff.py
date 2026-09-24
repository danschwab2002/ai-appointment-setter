from __future__ import annotations

import hashlib
import re
from typing import Protocol

from bridge.supabase import InboundCommercialCaseAdmissionResult


class InboundHandoffClient(Protocol):
    async def request_inbound_human_handoff(self, **kwargs: object) -> object: ...


# El motivo fino que compone el worker. La taxonomia cerrada de la RPC vive en
# primary_reason_code; esto es el detalle que hace legible una derivacion.
_DETAIL_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


def detail_reason_code(proposal: dict[str, object]) -> str | None:
    """El motivo del proposal, solo si la RPC lo va a aceptar.

    Un motivo mal formado nunca puede voltear la derivacion: en ese caso el
    handoff se pide igual, sin detalle. Perder el motivo es malo; perder la
    derivacion dejaria al contacto esperando a nadie.
    """
    candidate = proposal.get("reason_code")
    if not isinstance(candidate, str):
        return None
    if not _DETAIL_REASON_CODE.match(candidate):
        return None
    return candidate


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
        detail_reason_code=detail_reason_code(proposal),
        projection_policy_key=projection_policy_key,
        projection_policy_version=projection_policy_version,
        now=now,
    )
