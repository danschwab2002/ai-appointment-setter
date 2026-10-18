"""A1 of the ATT1 cart: the first touch with the personal coupon and the link.

Decided by Dan on 2026-10-18 (decision «A1 espera al cupón, que dura 24 h, en
la hora del país»): A1 goes out 60 minutes after the abandonment with the
template ``att1_carrito_descuento_01``, the personal coupon of the case and a
checkout button whose suffix is OUR issued link plus ``&offDiscount=<code>``.

When the coupon is not ``valid`` yet, A1 waits a few minutes. The base gives a
first contact three retries (``max_execution_retries`` = 3 in att1-db,
measured on 2026-10-18) and a fourth failure would close it for good, with no
message at all. So A1 waits only on its first ``defer_attempts`` attempts and
within ``max_defer`` of its due time; after that it falls back to the template
of today, without discount. A1 is never lost because of the coupon.

This module only decides and builds; the dispatcher (``worker.py``) owns the
attempt, the final gate and the send.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from bridge.approved_templates import ApprovedTemplateError, parse_approved_template
from bridge.cart_discount import cart_discount_values, country_for_phone
from bridge.followup_discount import followup_button_suffix

logger = logging.getLogger(__name__)

CART_ABANDONMENT_TRIGGER = "cart_abandonment"
CART_DISCOUNT_VARIABLES = ("nombre", "producto", "cuando", "precio", "vence")
RECOVERY_COUPON_NOT_READY = "recovery_coupon_not_ready"

# A coupon in these states will not become valid by waiting.
_COUPON_DEAD = frozenset({"create_failed", "expired", "redeemed"})
# The reservation said no for a reason of the case: A1 must not go out at all.
_STOP_OUTCOMES = frozenset({
    "purchase_already_approved", "blocked_case", "blocked_contact",
    "blocked_opt_out", "blocked_intent",
})
# This attempt already used its link: never send a second one.
_REPLAY_OUTCOMES = frozenset({
    "request_started_replay", "already_accepted", "delivery_unknown",
})


@dataclass(frozen=True)
class CartDiscountConfig:
    template_name: str
    language: str
    price: str
    currency: str
    category: str = "MARKETING"
    defer_delay: timedelta = timedelta(minutes=5)
    max_defer: timedelta = timedelta(minutes=15)
    defer_attempts: int = 2

    def __post_init__(self) -> None:
        if not self.template_name.strip() or not self.language.strip():
            raise ValueError("cart_discount_template_invalid")
        if self.category not in {"MARKETING", "UTILITY"}:
            raise ValueError("cart_discount_category_invalid")
        if self.defer_attempts < 0 or self.defer_delay <= timedelta(0):
            raise ValueError("cart_discount_defer_invalid")


@dataclass(frozen=True)
class CartDiscountReady:
    message: str
    template_name: str
    template_params: dict[str, object]
    issuance_id: str


@dataclass(frozen=True)
class CartDiscountDefer:
    retry_after: timedelta
    reason: str = RECOVERY_COUPON_NOT_READY


@dataclass(frozen=True)
class CartDiscountStop:
    """A1 does not go out: the attempt closes with ``reason``."""

    reason: str
    retryable: bool


@dataclass(frozen=True)
class CartDiscountFallback:
    """A1 goes out with the template of today; ``reason`` goes to the log."""

    reason: str


CartDiscountOutcome = (
    CartDiscountReady | CartDiscountDefer | CartDiscountStop | CartDiscountFallback
)


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("cart_discount_naive_datetime")
    return parsed


async def compose_cart_discount_first_touch(
    *,
    config: CartDiscountConfig,
    supabase: object,
    inbox: Mapping[str, object],
    action: object,
    attempt: object,
    buyer_name: str,
    product_name: str,
    buyer_phone: str | None,
    worker_id: str,
    now: str,
    ulid_factory: Callable[[], str],
) -> CartDiscountOutcome:
    """Decide A1 for one cart action and build it when the coupon is ready."""
    at = _aware(now)
    coupon = await supabase.get_recovery_coupon_for_case(  # type: ignore[attr-defined]
        recovery_case_id=action.recovery_case_id,  # type: ignore[attr-defined]
        ordinal=1,
    )
    if coupon is None or coupon.status != "valid" or coupon.expires_at <= at:
        if coupon is not None and coupon.status in _COUPON_DEAD:
            return CartDiscountFallback(f"recovery_coupon_{coupon.status}")
        due = _aware(action.due_at)  # type: ignore[attr-defined]
        if (
            attempt.attempt_number <= config.defer_attempts  # type: ignore[attr-defined]
            and at + config.defer_delay <= due + config.max_defer
        ):
            return CartDiscountDefer(retry_after=config.defer_delay)
        return CartDiscountFallback("recovery_coupon_not_ready_in_time")
    try:
        values = cart_discount_values(
            buyer_name=buyer_name,
            product_name=product_name,
            abandoned_at=_aware(action.anchor_observed_at),  # type: ignore[attr-defined]
            coupon_expires_at=coupon.expires_at,
            price=config.price,
            currency=config.currency,
            country=country_for_phone(buyer_phone),
            now=at,
        )
        ordered = {
            str(position): values[name]
            for position, name in enumerate(CART_DISCOUNT_VARIABLES, start=1)
        }
        approved = parse_approved_template(
            inbox,
            template_name=config.template_name,
            expected_language=config.language,
            expected_category=config.category,
            parameter_count=len(CART_DISCOUNT_VARIABLES),
            checkout_button=True,
        )
        message = approved.render(ordered)
    except ApprovedTemplateError as exc:
        return CartDiscountFallback(f"cart_discount_template_{exc.reason}")
    except ValueError as exc:
        return CartDiscountFallback(str(exc) or "cart_discount_values_invalid")
    reservation = await supabase.reserve_recovery_checkout_issuance(  # type: ignore[attr-defined]
        action_id=action.action_id,  # type: ignore[attr-defined]
        worker_id=worker_id,
        lease_generation=action.lease_generation,  # type: ignore[attr-defined]
        attempt_id=attempt.attempt_id,  # type: ignore[attr-defined]
        issuance_ulid=ulid_factory(),
        now=now,
    )
    if reservation.outcome in _STOP_OUTCOMES:
        return CartDiscountStop(
            reason=f"recovery_checkout_{reservation.outcome}", retryable=False
        )
    if reservation.outcome in _REPLAY_OUTCOMES:
        return CartDiscountStop(
            reason=f"recovery_checkout_{reservation.outcome}", retryable=False
        )
    if reservation.outcome != "reserved":
        # blocked_attempt, blocked_identity, blocked_binding, missing_offer: the
        # link cannot be issued, the message still can.
        return CartDiscountFallback(f"recovery_checkout_{reservation.outcome}")
    assert reservation.issuance_id is not None
    assert reservation.checkout_url_final is not None
    try:
        suffix = followup_button_suffix(reservation.checkout_url_final, coupon.code)
        processed = approved.processed_params(ordered, button_suffix=suffix)
    except (ValueError, ApprovedTemplateError) as exc:
        return CartDiscountFallback(str(exc) or "cart_discount_button_invalid")
    return CartDiscountReady(
        message=message,
        template_name=config.template_name,
        template_params={
            "name": config.template_name,
            "category": config.category,
            "language": config.language,
            "processed_params": processed,
        },
        issuance_id=reservation.issuance_id,
    )
