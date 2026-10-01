"""Two-phase, idempotent delivery of a durable checkout issuance."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from bridge.chatwoot import ChatwootProtocolError, ChatwootReplyDeliveryUnknownError
from bridge.checkout_issuance import generate_issuance_ulid
from bridge.payment_link import PaymentLinkUnavailable, render_payment_link_reply
from bridge.payment_link_template import (
    parse_payment_link_template,
    payment_link_button_suffix,
)
from bridge.supabase import SupabaseError


logger = logging.getLogger(__name__)


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
    link_template_name: str | None = None,
    link_template_language: str | None = None,
    part_delay_seconds: float = 0.0,
    part_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CheckoutDeliveryResult:
    """Reserve a stable URL, authorize at the last boundary, and send once.

    Con ``link_template_name`` el link sale en el boton de esa plantilla, en
    una segunda parte despues del texto del agente. La autorizacion de la
    emision va en la parte del link, como siempre, y el cierre de la emision
    queda atado a ese mensaje. Si la plantilla no esta disponible, el link sale
    escrito, en un solo mensaje, como antes.
    """

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

    link_part = await _resolve_link_template_part(
        control_client=control_client,
        inbox_id=chatwoot_inbox_id,
        template_name=link_template_name,
        expected_language=link_template_language,
        final_url=reservation.checkout_url_final,
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

    if link_part is not None:
        # Primero el texto del agente, sin link y sin autorizar la emision: la
        # autorizacion cuida al link, que va en la segunda parte.
        preamble_part = preamble.rstrip()
        template_content, template_params = link_part
        first_args = {
            key: value
            for key, value in send_args.items()
            if key != "pre_send_authorizer"
        }
        first_args.update(
            {
                "content": preamble_part,
                "part_index": 1,
                "part_count": 2,
                "prior_parts": (),
            }
        )
        try:
            first = await control_client.send_agent_bot_reply(**first_args)
        except ChatwootReplyDeliveryUnknownError as exc:
            raise CheckoutDeliveryError("checkout_issuance_delivery_unknown") from exc
        except (ChatwootProtocolError, httpx.HTTPError) as exc:
            raise CheckoutDeliveryError("checkout_issuance_send_unconfirmed") from exc
        first_status = first.get("status")
        if first_status == "blocked":
            reason = first.get("reason")
            return CheckoutDeliveryResult(
                "blocked", reason if isinstance(reason, str) else "chatwoot_blocked",
                reservation.issuance_id, None,
            )
        if first_status not in {"sent", "duplicate"}:
            raise CheckoutDeliveryError("checkout_issuance_invalid_chatwoot_result")
        # La misma pausa que entre las partes de una respuesta larga: Chatwoot
        # manda cada mensaje a WhatsApp desde su cola, y sin pausa la plantilla
        # puede llegar antes que el texto. En un reintento el texto ya salio.
        if first_status == "sent" and part_delay_seconds > 0:
            await part_sleep(part_delay_seconds)
        send_args.update(
            {
                "content": template_content,
                "part_index": 2,
                "part_count": 2,
                "prior_parts": (preamble_part,),
                "template_params": template_params,
            }
        )

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


async def _resolve_link_template_part(
    *,
    control_client: Any,
    inbox_id: int,
    template_name: str | None,
    expected_language: str | None,
    final_url: str,
) -> tuple[str, dict[str, object]] | None:
    """El contenido y los parametros de la plantilla, o ``None`` para el texto.

    Ante cualquier duda el link sale escrito, como antes: el lead no se queda
    sin link porque la plantilla no este aprobada, falte en el catalogo de
    Chatwoot o el link no entre en el boton. El motivo queda en un WARNING,
    porque el bridge no muestra los INFO.
    """
    if template_name is None:
        return None
    try:
        inbox = await control_client.get_inbox(inbox_id=inbox_id)
        template = parse_payment_link_template(
            inbox,
            template_name=template_name,
            expected_language=expected_language,
        )
        button_suffix = payment_link_button_suffix(final_url)
        return (
            template.content(final_url=final_url),
            template.params(button_suffix=button_suffix),
        )
    except (ChatwootProtocolError, ValueError) as exc:
        reason = exc.args[0] if exc.args else type(exc).__name__
        logger.warning("payment_link_template_unavailable reason=%s", reason)
    except httpx.HTTPError as exc:
        logger.warning(
            "payment_link_template_unavailable reason=%s", type(exc).__name__
        )
    return None
