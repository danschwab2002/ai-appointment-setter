"""Two-phase, idempotent delivery of a durable checkout issuance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from bridge.chatwoot import ChatwootProtocolError, ChatwootReplyDeliveryUnknownError
from bridge.checkout_issuance import generate_issuance_ulid
from bridge.payment_link import PaymentLinkUnavailable, render_payment_link_reply
from bridge.supabase import SupabaseError


@dataclass(frozen=True)
class CheckoutDeliveryResult:
    outcome: str
    reason: str | None
    issuance_id: str | None
    chatwoot_message_id: int | None


class CheckoutDeliveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def deliver_checkout_issuance_v2(
    *,
    supabase: Any,
    control_client: Any,
    commercial_case_id: str,
    external_user_id: str,
    chatwoot_account_id: int,
    chatwoot_inbox_id: int,
    chatwoot_conversation_id: int,
    trigger_message_id: int,
    delivery_id: str,
    preamble: str,
    expected_jid: str | None = None,
) -> CheckoutDeliveryResult:
    """Reserve a stable URL, authorize at the last boundary, and send once."""

    context = {
        "external_user_id": external_user_id,
        "chatwoot_account_id": chatwoot_account_id,
        "chatwoot_inbox_id": chatwoot_inbox_id,
        "chatwoot_conversation_id": chatwoot_conversation_id,
        "trigger_external_message_id": str(trigger_message_id),
    }
    try:
        reservation = await supabase.reserve_chatwoot_checkout_issuance_v2(
            commercial_case_id=commercial_case_id,
            issuance_ulid=generate_issuance_ulid(),
            now=datetime.now(UTC).isoformat(),
            **context,
        )
    except SupabaseError as exc:
        raise CheckoutDeliveryError("checkout_issuance_reserve_failed") from exc

    if reservation.outcome not in {
        "reserved", "request_started_replay", "already_accepted", "delivery_unknown"
    }:
        return CheckoutDeliveryResult(
            "blocked", reservation.outcome, reservation.issuance_id, None
        )
    if reservation.issuance_id is None or reservation.checkout_url_final is None:
        raise CheckoutDeliveryError("checkout_issuance_reservation_incomplete")

    try:
        content = render_payment_link_reply(
            preamble=preamble,
            final_url=reservation.checkout_url_final,
        )
    except PaymentLinkUnavailable:
        return CheckoutDeliveryResult(
            "blocked", "payment_link_reply_rejected", reservation.issuance_id, None
        )

    authorization = None

    async def authorize() -> bool:
        nonlocal authorization
        try:
            authorization = await supabase.authorize_chatwoot_checkout_issuance_v2(
                issuance_id=reservation.issuance_id,
                now=datetime.now(UTC).isoformat(),
                **context,
            )
        except SupabaseError as exc:
            raise CheckoutDeliveryError("checkout_issuance_authorize_failed") from exc
        return authorization.outcome == "request_started"

    send_args: dict[str, Any] = {
        "conversation_id": chatwoot_conversation_id,
        "expected_inbox_id": chatwoot_inbox_id,
        "trigger_message_id": trigger_message_id,
        "delivery_id": delivery_id,
        "content": content,
        "pre_send_authorizer": authorize,
    }
    if expected_jid is not None:
        send_args["expected_jid"] = expected_jid

    try:
        result = await control_client.send_agent_bot_reply(**send_args)
    except ChatwootReplyDeliveryUnknownError as exc:
        if authorization is not None and authorization.outcome == "request_started":
            try:
                await supabase.finalize_chatwoot_checkout_issuance_v2(
                    issuance_id=reservation.issuance_id,
                    status="delivery_unknown",
                    chatwoot_message_id=None,
                    failure_code="chatwoot_delivery_unknown",
                    now=datetime.now(UTC).isoformat(),
                )
            except SupabaseError as finalize_exc:
                raise CheckoutDeliveryError(
                    "checkout_issuance_delivery_unknown_finalize_failed"
                ) from finalize_exc
        raise CheckoutDeliveryError("checkout_issuance_delivery_unknown") from exc
    except (ChatwootProtocolError, httpx.HTTPError) as exc:
        if authorization is not None and authorization.outcome == "request_started":
            try:
                await supabase.finalize_chatwoot_checkout_issuance_v2(
                    issuance_id=reservation.issuance_id,
                    status="delivery_unknown",
                    chatwoot_message_id=None,
                    failure_code="chatwoot_send_unconfirmed",
                    now=datetime.now(UTC).isoformat(),
                )
            except SupabaseError as finalize_exc:
                raise CheckoutDeliveryError(
                    "checkout_issuance_delivery_unknown_finalize_failed"
                ) from finalize_exc
        raise CheckoutDeliveryError("checkout_issuance_send_unconfirmed") from exc

    result_status = result.get("status")
    result_message_id = result.get("message_id")
    confirmed = (
        result_status in {"sent", "duplicate"}
        and isinstance(result_message_id, int)
        and not isinstance(result_message_id, bool)
        and result_message_id > 0
    )
    if result_status == "blocked":
        reason = result.get("reason")
        return CheckoutDeliveryResult(
            "blocked", reason if isinstance(reason, str) else "chatwoot_blocked",
            reservation.issuance_id, None,
        )
    if not confirmed:
        raise CheckoutDeliveryError("checkout_issuance_invalid_chatwoot_result")

    if authorization is None:
        await authorize()
    assert authorization is not None

    if authorization.outcome == "already_accepted":
        return CheckoutDeliveryResult(
            "replayed", None, reservation.issuance_id, result_message_id
        )
    if authorization.outcome in {"delivery_unknown", "request_started_replay"}:
        if result_status != "duplicate":
            return CheckoutDeliveryResult(
                "blocked", authorization.outcome,
                reservation.issuance_id, result_message_id,
            )
    elif authorization.outcome != "request_started":
        return CheckoutDeliveryResult(
            "blocked", authorization.outcome,
            reservation.issuance_id, result_message_id,
        )

    try:
        await supabase.finalize_chatwoot_checkout_issuance_v2(
            issuance_id=reservation.issuance_id,
            status="accepted_by_chatwoot",
            chatwoot_message_id=result_message_id,
            failure_code=None,
            now=datetime.now(UTC).isoformat(),
        )
    except SupabaseError as exc:
        raise CheckoutDeliveryError("checkout_issuance_acceptance_finalize_failed") from exc

    outcome = "reconciled" if result_status == "duplicate" else "sent"
    return CheckoutDeliveryResult(
        outcome, None, reservation.issuance_id, result_message_id
    )
