"""The personal coupon of a native step (E-483, C2; migration 20261112210500).

A native step whose button is ``{link: checkout, cupon: personal}`` goes out
with the person's own coupon, like A1 and Z of the ATT1 cart (D3 of the map of
2026-11-12: 10 %, two per case at most, 24 h, said in the time of the
person's country). Nothing here creates a coupon: the base seeds the row from
the policy of the step (``coupon_ordinal``) and the coupon worker of ATT1
(``recovery_coupons.py``) creates and verifies it in Hotmart, unchanged. The
dispatcher reads the ordinal and the coupon of its step
(``get_flow_engine_native_step_coupon_v1``) and waits for it the way Z waits
for coupon 2 (``cart_followups.compose_cart_last_touch``): a native step has
no template without the discount to fall back to, so a coupon that never gets
ready closes the step without a message.

The variables the coupon brings, by manifest name: ``vence`` (when the coupon
expires), ``precio`` (the price of the instance, as A1 says it) and, only in
the first send, ``cuando`` (the moment of the event; a following touch is
anchored to the previous message, not to the event). ``nombre`` and
``producto`` stay with the dispatcher.

This module only decides; the dispatcher (``worker.py``) owns the attempt,
the link and the send. Only with FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bridge.approved_templates import REASON_PARAMETERS_MISSING
from bridge.cart_discount import country_for_phone, format_moment, format_price, timezone_for_country
from bridge.cart_first_touch import CartDiscountDefer, CartDiscountStop
from bridge.supabase import SupabaseError

__all__ = [
    "NATIVE_COUPON_NOT_DECLARED",
    "NATIVE_COUPON_NOT_READY_IN_TIME",
    "NATIVE_COUPON_UNAVAILABLE",
    "NATIVE_COUPON_VARIABLES",
    "NATIVE_PERSONAL_COUPON_DISABLED",
    "NativeCouponReady",
    "NativePersonalCouponConfig",
    "resolve_native_personal_coupon",
]

# The step carries the personal coupon and the instance does not build it.
NATIVE_PERSONAL_COUPON_DISABLED = "flow_engine_native_personal_coupon_disabled"
# The read of the step coupon did not answer: retried.
NATIVE_COUPON_UNAVAILABLE = "flow_engine_native_coupon_unavailable"
# The definition says personal coupon and the policy of the step does not: never goes out.
NATIVE_COUPON_NOT_DECLARED = "flow_engine_native_coupon_not_declared"
NATIVE_COUPON_NOT_READY_IN_TIME = "flow_engine_native_coupon_not_ready_in_time"
RECOVERY_COUPON_NOT_READY = "recovery_coupon_not_ready"
# What a step with the personal coupon can say, besides nombre and producto.
NATIVE_COUPON_VARIABLES = frozenset({"cuando", "precio", "vence"})

# A coupon in these states will not become valid by waiting.
_COUPON_DEAD = frozenset({"create_failed", "expired", "redeemed"})


@dataclass(frozen=True)
class NativePersonalCouponConfig:
    """How a native step waits for its coupon, and the price it says.

    The waits are those of Z (``CartFollowupConfig``): Hotmart applies a
    coupon about 5 minutes after creating it, and the base seeds it 15 minutes
    before the step is due. ``price`` is the instance's (RECOVERY_DISCOUNT_PRICE
    and _CURRENCY, the one A1 says); without it a step with ``precio`` closes.
    """

    price: str | None
    currency: str = "USD"
    defer_delay: timedelta = timedelta(minutes=8)
    max_defer: timedelta = timedelta(minutes=30)
    defer_attempts: int = 2

    def __post_init__(self) -> None:
        if self.defer_attempts < 0 or self.defer_delay <= timedelta(0) or self.max_defer < timedelta(0):
            raise ValueError("native_personal_coupon_defer_invalid")
        if self.price is not None:
            format_price(self.price, self.currency)


@dataclass(frozen=True)
class NativeCouponReady:
    """The coupon is valid: its code for the button and the values of its variables."""

    code: str
    ordinal: int
    values: dict[str, str]


NativeCouponOutcome = NativeCouponReady | CartDiscountDefer | CartDiscountStop


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("native_personal_coupon_naive_datetime")
    return parsed


async def resolve_native_personal_coupon(
    *,
    config: NativePersonalCouponConfig,
    supabase: object,
    action: object,
    attempt: object,
    variables: tuple[str, ...],
    buyer_phone: str | None,
    following: bool,
    now: str | datetime,
) -> NativeCouponOutcome:
    """The coupon of a native step, or how long to wait for it, or why it does not go out."""
    at = _aware(now)
    try:
        step = await supabase.get_flow_engine_native_step_coupon(  # type: ignore[attr-defined]
            action_id=action.action_id,  # type: ignore[attr-defined]
        )
    except SupabaseError:
        return CartDiscountStop(reason=NATIVE_COUPON_UNAVAILABLE, retryable=True)
    if step is None:
        return CartDiscountStop(reason=NATIVE_COUPON_NOT_DECLARED, retryable=False)
    coupon = step.coupon
    if coupon is None or coupon.status != "valid" or coupon.expires_at <= at:
        if coupon is not None and coupon.status in _COUPON_DEAD:
            return CartDiscountStop(reason=f"recovery_coupon_{coupon.status}", retryable=False)
        due = _aware(action.due_at)  # type: ignore[attr-defined]
        if (
            attempt.attempt_number <= config.defer_attempts  # type: ignore[attr-defined]
            and at + config.defer_delay <= due + config.max_defer
        ):
            return CartDiscountDefer(retry_after=config.defer_delay, reason=RECOVERY_COUPON_NOT_READY)
        return CartDiscountStop(reason=NATIVE_COUPON_NOT_READY_IN_TIME, retryable=False)
    zone = timezone_for_country(country_for_phone(buyer_phone))
    values: dict[str, str] = {}
    for name in variables:
        if name == "vence":
            values[name] = format_moment(coupon.expires_at, now=at, zone=zone)
        elif name == "precio":
            if config.price is None:
                return CartDiscountStop(reason=REASON_PARAMETERS_MISSING, retryable=False)
            values[name] = format_price(config.price, config.currency)
        elif name == "cuando":
            if following:
                # The base never compiles cuando after the first send: its anchor is the previous message.
                return CartDiscountStop(reason=NATIVE_COUPON_NOT_DECLARED, retryable=False)
            values[name] = format_moment(
                _aware(action.anchor_observed_at),  # type: ignore[attr-defined]
                now=at,
                zone=zone,
            )
    return NativeCouponReady(
        code=coupon.code,
        ordinal=step.ordinal,
        values={key: " ".join(value.split()) for key, value in values.items()},
    )
