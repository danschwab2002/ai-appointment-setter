"""Las RPC del correlador que decide solo (identity-link-decider-v1, E-536).

Un adaptador sobre ``SupabaseClient`` en su propio modulo, para no sumar
superficie a ``supabase.py`` (lo comparte con el resto del bridge). Todas las
lecturas fallan cerradas: una forma inesperada es ``SupabaseError``.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any
from uuid import UUID

from bridge.identity_link import (
    IdentityLinkClaim,
    IdentityLinkNotice,
    IdentityLinkVerdict,
    IdentityQuestionClaim,
)
from bridge.supabase import SupabaseClient, SupabaseError, _response_rows


def _uuid(value: object, *, operation: str) -> str:
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise SupabaseError(f"{operation}_invalid_shape") from exc
    if str(parsed) != value:
        raise SupabaseError(f"{operation}_invalid_shape")
    return str(parsed)


def _int(value: object, *, operation: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SupabaseError(f"{operation}_invalid_shape")
    return value


class IdentityLinkSupabaseStore:
    """La cola, la decision, la pregunta, la respuesta y los avisos, por RPC."""

    def __init__(self, supabase: SupabaseClient) -> None:
        self._supabase = supabase

    async def _rpc(self, operation: str, name: str, body: dict[str, Any]) -> Any:
        response = await self._supabase._request(
            "POST", f"/rest/v1/rpc/{name}", content=json.dumps(body, ensure_ascii=False)
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        return response

    @staticmethod
    def _scalar(response: Any, *, operation: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SupabaseError(f"{operation}_invalid_json") from exc

    # -- La cola y la decision -------------------------------------------------

    async def claim_identity_link_case(
        self, *, tenant_ref: str, funnel_ref: str, worker_id: str
    ) -> IdentityLinkClaim | None:
        operation = "identity_link_claim"
        response = await self._rpc(operation, "claim_identity_link_cases_v1", {
            "p_tenant_ref": tenant_ref,
            "p_funnel_ref": funnel_ref,
            "p_worker_id": worker_id,
            "p_lease_seconds": 120,
        })
        rows = _response_rows(response, operation=operation)
        if not rows:
            return None
        if len(rows) > 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        if set(row) != {"webhook_event_id", "claim_token", "lease_generation", "attempt_count",
                        "evidence", "evidence_sha256"} or not isinstance(row["evidence"], dict):
            raise SupabaseError(f"{operation}_invalid_shape")
        return IdentityLinkClaim(
            webhook_event_id=_uuid(row["webhook_event_id"], operation=operation),
            claim_token=_uuid(row["claim_token"], operation=operation),
            lease_generation=_int(row["lease_generation"], operation=operation),
            attempt_count=_int(row["attempt_count"], operation=operation),
            evidence=row["evidence"],
            evidence_sha256=str(row["evidence_sha256"]),
        )

    async def complete_identity_link_decision(
        self, *, claim: IdentityLinkClaim, mode: str, verdict: IdentityLinkVerdict
    ) -> dict[str, object]:
        operation = "identity_link_complete"
        response = await self._rpc(operation, "complete_identity_link_decision_v1", {
            "p_webhook_event_id": claim.webhook_event_id,
            "p_claim_token": claim.claim_token,
            "p_lease_generation": claim.lease_generation,
            "p_mode": mode,
            "p_decided_by": verdict.decided_by,
            "p_branch": verdict.branch,
            "p_guard_reason": verdict.guard_reason,
            "p_model_decision": verdict.model_decision,
            "p_model_confidence": verdict.model_confidence,
            "p_reason_codes": list(verdict.reason_codes),
            "p_prompt_version": verdict.prompt_version,
            "p_destination_source": verdict.destination_source,
        })
        result = self._scalar(response, operation=operation)
        if not isinstance(result, dict) or result.get("branch") not in {"ata", "pregunta", "nada"}:
            raise SupabaseError(f"{operation}_invalid_shape")
        return result

    async def release_identity_link_case(self, *, claim: IdentityLinkClaim, failure_code: str) -> None:
        await self._rpc("identity_link_release", "release_identity_link_case_v1", {
            "p_webhook_event_id": claim.webhook_event_id,
            "p_claim_token": claim.claim_token,
            "p_lease_generation": claim.lease_generation,
            "p_failure_code": failure_code,
        })

    async def requeue_identity_linked_events(self, *, tenant_ref: str, funnel_ref: str) -> int:
        operation = "identity_link_requeue"
        response = await self._rpc(operation, "requeue_identity_linked_events_v1", {
            "p_tenant_ref": tenant_ref, "p_funnel_ref": funnel_ref,
        })
        return _int(self._scalar(response, operation=operation), operation=operation)

    # -- La resolucion del carrito atado (resolution.py) ------------------------

    async def bind_identity_link_contact(self, *, webhook_event_id: str, contact_id: str) -> str | None:
        """El telefono al que escribe el carrito atado (y el contacto con el), o None."""
        operation = "identity_link_bind_contact"
        response = await self._rpc(operation, "bind_identity_link_contact_v1", {
            "p_webhook_event_id": webhook_event_id, "p_contact_id": contact_id,
        })
        value = self._scalar(response, operation=operation)
        if value is not None and (not isinstance(value, str) or not value.isdigit()):
            raise SupabaseError(f"{operation}_invalid_shape")
        return value

    # -- Los avisos -------------------------------------------------------------

    async def read_identity_link_notices(
        self, *, tenant_ref: str, funnel_ref: str, limit: int
    ) -> list[IdentityLinkNotice]:
        operation = "identity_link_notices"
        response = await self._rpc(operation, "read_identity_link_notices_v1", {
            "p_tenant_ref": tenant_ref, "p_funnel_ref": funnel_ref, "p_limit": limit,
        })
        notices = []
        for row in _response_rows(response, operation=operation):
            if set(row) != {"notice_key", "kind", "branch", "decided_by", "outcome", "conversation_id", "occurred_at"}:
                raise SupabaseError(f"{operation}_invalid_shape")
            conversation = row["conversation_id"]
            if conversation is not None:
                conversation = _int(conversation, operation=operation)
            try:
                occurred_at = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise SupabaseError(f"{operation}_invalid_shape") from exc
            if row["kind"] not in {"decision", "envio"} or row["outcome"] not in {None, "sale", "falla"}:
                raise SupabaseError(f"{operation}_invalid_shape")
            notices.append(IdentityLinkNotice(
                notice_key=str(row["notice_key"]), kind=row["kind"], branch=str(row["branch"]),
                decided_by=str(row["decided_by"]), outcome=row["outcome"], conversation_id=conversation,
                occurred_at=occurred_at,
            ))
        return notices

    async def mark_identity_link_notice(self, *, tenant_ref: str, funnel_ref: str, notice_key: str) -> bool:
        operation = "identity_link_notice_mark"
        response = await self._rpc(operation, "mark_identity_link_notice_v1", {
            "p_tenant_ref": tenant_ref, "p_funnel_ref": funnel_ref, "p_notice_key": notice_key,
        })
        return self._scalar(response, operation=operation) is True

    # -- Bloque B: la pregunta y la respuesta -----------------------------------

    async def expire_identity_link_questions(self, *, tenant_ref: str, funnel_ref: str) -> int:
        operation = "identity_link_question_expire"
        response = await self._rpc(operation, "expire_identity_link_questions_v1", {
            "p_tenant_ref": tenant_ref, "p_funnel_ref": funnel_ref,
        })
        return _int(self._scalar(response, operation=operation), operation=operation)

    async def claim_identity_link_question(
        self, *, tenant_ref: str, funnel_ref: str, worker_id: str
    ) -> IdentityQuestionClaim | None:
        operation = "identity_link_question_claim"
        response = await self._rpc(operation, "claim_identity_link_question_v1", {
            "p_tenant_ref": tenant_ref, "p_funnel_ref": funnel_ref, "p_worker_id": worker_id,
            "p_lease_seconds": 120,
        })
        rows = _response_rows(response, operation=operation)
        if not rows:
            return None
        row = rows[0]
        if len(rows) > 1 or set(row) != {"decision_id", "claim_token", "lease_generation", "phone",
                                          "full_name", "product_name"}:
            raise SupabaseError(f"{operation}_invalid_shape")
        phone = row["phone"]
        if not isinstance(phone, str) or not phone.isdigit():
            raise SupabaseError(f"{operation}_invalid_shape")
        return IdentityQuestionClaim(
            decision_id=_uuid(row["decision_id"], operation=operation),
            claim_token=_uuid(row["claim_token"], operation=operation),
            lease_generation=_int(row["lease_generation"], operation=operation),
            phone=phone,
            full_name=row["full_name"] if isinstance(row["full_name"], str) else None,
            product_name=row["product_name"] if isinstance(row["product_name"], str) else None,
        )

    async def start_identity_link_question(self, *, claim: IdentityQuestionClaim) -> str:
        operation = "identity_link_question_start"
        response = await self._rpc(operation, "start_identity_link_question_v1", {
            "p_decision_id": claim.decision_id, "p_claim_token": claim.claim_token,
            "p_lease_generation": claim.lease_generation,
        })
        value = self._scalar(response, operation=operation)
        if not isinstance(value, str):
            raise SupabaseError(f"{operation}_invalid_shape")
        return value

    async def finish_identity_link_question(
        self,
        *,
        claim: IdentityQuestionClaim,
        outcome: str,
        conversation_id: int | None,
        message_id: int | None,
        failure_code: str | None,
    ) -> None:
        await self._rpc("identity_link_question_finish", "finish_identity_link_question_v1", {
            "p_decision_id": claim.decision_id, "p_claim_token": claim.claim_token,
            "p_lease_generation": claim.lease_generation, "p_outcome": outcome,
            "p_conversation_id": conversation_id, "p_message_id": message_id,
            "p_failure_code": failure_code,
        })

    async def record_identity_link_answer(
        self, *, tenant_ref: str, funnel_ref: str, conversation_id: int, answer: str, message_id: int | None
    ) -> dict[str, object]:
        operation = "identity_link_answer"
        response = await self._rpc(operation, "record_identity_link_answer_v1", {
            "p_tenant_ref": tenant_ref, "p_funnel_ref": funnel_ref, "p_conversation_id": conversation_id,
            "p_answer": answer, "p_message_id": message_id,
        })
        value = self._scalar(response, operation=operation)
        if not isinstance(value, dict):
            raise SupabaseError(f"{operation}_invalid_shape")
        return value
