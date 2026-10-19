"""A2 and Z of the ATT1 cart: the second and the last touch.

Contract: ``docs/contracts/portable-cart-followups-v1.md``.
Specified in «los flujos comerciales de ATT1 como tienen que quedar», flow 3
and Z (2026-10-17), and in the decision of 2026-10-18 «A2 es uno solo»:

- A2 (``cart_second_touch``), 24 h after A1: the doubts and the payment, with
  quick replies only («Quiero mi 10 %», «No más mensajes»);
- Z (``cart_last_touch``), 72 h after A1: the only renewal of the case, with
  coupon 2 and our link in the checkout button.

The steps live in the cart policy of the instance after the first contact;
the engine inserts each one when the previous one is accepted. «Quiero mi
10 %» on A2 brings Z forward (``request_recovery_renewal_v1``): Z is the renewal
with the payment ready, so the agent never creates coupons and there is no
«Z without a code».

This module only decides and builds; the dispatcher (``worker.py``) owns the
attempt, the final gate and the send.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from bridge.approved_templates import (
    REASON_PARAMETERS_MISSING,
    ApprovedTemplateError,
    parse_approved_template,
)
from bridge.cart_discount import country_for_phone, format_moment, timezone_for_country
from bridge.cart_first_touch import (
    CART_ABANDONMENT_TRIGGER,
    CartDiscountDefer,
    CartDiscountStop,
)
from bridge.followup_discount import followup_button_suffix
from bridge.messaging import FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED

__all__ = [
    "CART_FOLLOWUP_STEPS",
    "CART_LAST_TOUCH_STEP",
    "CART_RENEWAL_COUPON_ORDINAL",
    "CART_SECOND_TOUCH_STEP",
    "CART_STEP_TRIGGERS",
    "CartFollowupConfig",
    "CartFollowupOutcome",
    "CartFollowupReady",
    "compose_cart_last_touch",
    "compose_cart_second_touch",
    "is_cart_renewal_request",
]

# The step keys of the cart policy after the first contact (the policy the
# instance publishes writes them in this order).
CART_SECOND_TOUCH_STEP = "cart_second_touch"
CART_LAST_TOUCH_STEP = "cart_last_touch"
CART_FOLLOWUP_STEPS = (CART_SECOND_TOUCH_STEP, CART_LAST_TOUCH_STEP)
CART_STEP_TRIGGERS: Mapping[str, str] = {
    step_key: CART_ABANDONMENT_TRIGGER for step_key in CART_FOLLOWUP_STEPS
}

CART_SECOND_TOUCH_VARIABLES = ("nombre", "producto")
CART_LAST_TOUCH_VARIABLES = ("nombre", "producto", "vence")
# A1 uses coupon 1; Z is the only renewal of the case.
CART_RENEWAL_COUPON_ORDINAL = 2

RECOVERY_COUPON_NOT_READY = "recovery_coupon_not_ready"
CART_LAST_TOUCH_COUPON_NOT_READY_IN_TIME = "cart_last_touch_coupon_not_ready_in_time"

# A coupon in these states will not become valid by waiting.
_COUPON_DEAD = frozenset({"create_failed", "expired", "redeemed"})

_RENEWAL_REPLY = re.compile(r"quiero mi 10 ?%")
_QUOTES = "\"'`«»‹›“”‘’„＂「」『』"


@dataclass(frozen=True)
class CartFollowupConfig:
    """The approved templates of A2 and Z, and how long Z waits for coupon 2.

    A step without its template does not go out. Z waits ``defer_delay`` at a
    time, on its first ``defer_attempts`` attempts and within ``max_defer`` of
    its due time: a Z brought forward by «Quiero mi 10 %» finds coupon 2 just
    seeded, and Hotmart applies a coupon about 5 minutes after creating it.
    """

    language: str
    second_touch_template: str | None = None
    last_touch_template: str | None = None
    category: str = "MARKETING"
    defer_delay: timedelta = timedelta(minutes=8)
    max_defer: timedelta = timedelta(minutes=30)
    defer_attempts: int = 2

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise ValueError("cart_followup_language_invalid")
        for name in (self.second_touch_template, self.last_touch_template):
            if name is not None and not name.strip():
                raise ValueError("cart_followup_template_invalid")
        if self.category not in {"MARKETING", "UTILITY"}:
            raise ValueError("cart_followup_category_invalid")
        if self.defer_attempts < 0 or self.defer_delay <= timedelta(0):
            raise ValueError("cart_followup_defer_invalid")

    def template_for(self, step_key: str) -> str | None:
        if step_key == CART_SECOND_TOUCH_STEP:
            return self.second_touch_template
        if step_key == CART_LAST_TOUCH_STEP:
            return self.last_touch_template
        return None


@dataclass(frozen=True)
class CartFollowupReady:
    """The step is built. ``issuance_id`` is the link of Z; A2 has none."""

    message: str
    template_name: str
    template_params: dict[str, object]
    issuance_id: str | None = None


# A2 and Z reuse the defer and the stop of A1. There is no fallback: a step
# that cannot be built closes without a message (``CartDiscountStop``).
CartFollowupOutcome = CartFollowupReady | CartDiscountDefer | CartDiscountStop


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("cart_followup_naive_datetime")
    return parsed


def _clean(value: str) -> str:
    return " ".join(value.split())


def compose_cart_second_touch(
    *,
    config: CartFollowupConfig,
    inbox: Mapping[str, object],
    buyer_name: str,
    product_name: str,
) -> CartFollowupOutcome:
    """A2: the approved template with the name and the product, no link."""
    template_name = config.second_touch_template
    if template_name is None:
        return CartDiscountStop(reason=FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED, retryable=False)
    values = {"nombre": _clean(buyer_name), "producto": _clean(product_name)}
    if not all(values.values()):
        return CartDiscountStop(reason=REASON_PARAMETERS_MISSING, retryable=False)
    ordered = {
        str(position): values[name]
        for position, name in enumerate(CART_SECOND_TOUCH_VARIABLES, start=1)
    }
    try:
        approved = parse_approved_template(
            inbox,
            template_name=template_name,
            expected_language=config.language,
            expected_category=config.category,
            parameter_count=len(CART_SECOND_TOUCH_VARIABLES),
        )
        message = approved.render(ordered)
        processed = approved.processed_params(ordered)
    except ApprovedTemplateError as exc:
        # A value the render refuses comes from the case and does not change;
        # a catalog that does not close can.
        return CartDiscountStop(
            reason=exc.reason,
            retryable=exc.reason != REASON_PARAMETERS_MISSING,
        )
    return CartFollowupReady(
        message=message,
        template_name=template_name,
        template_params={
            "name": template_name,
            "category": config.category,
            "language": config.language,
            "processed_params": processed,
        },
    )


async def compose_cart_last_touch(
    *,
    config: CartFollowupConfig,
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
) -> CartFollowupOutcome:
    """Z: coupon 2 and our link in the button, or no message at all."""
    template_name = config.last_touch_template
    if template_name is None:
        return CartDiscountStop(reason=FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED, retryable=False)
    at = _aware(now)
    coupon = await supabase.get_recovery_coupon_for_case(  # type: ignore[attr-defined]
        recovery_case_id=action.recovery_case_id,  # type: ignore[attr-defined]
        ordinal=CART_RENEWAL_COUPON_ORDINAL,
    )
    if coupon is None or coupon.status != "valid" or coupon.expires_at <= at:
        if coupon is not None and coupon.status in _COUPON_DEAD:
            return CartDiscountStop(
                reason=f"recovery_coupon_{coupon.status}", retryable=False
            )
        due = _aware(action.due_at)  # type: ignore[attr-defined]
        if (
            attempt.attempt_number <= config.defer_attempts  # type: ignore[attr-defined]
            and at + config.defer_delay <= due + config.max_defer
        ):
            return CartDiscountDefer(
                retry_after=config.defer_delay, reason=RECOVERY_COUPON_NOT_READY
            )
        return CartDiscountStop(
            reason=CART_LAST_TOUCH_COUPON_NOT_READY_IN_TIME, retryable=False
        )
    values = {
        "nombre": _clean(buyer_name),
        "producto": _clean(product_name),
        "vence": format_moment(
            coupon.expires_at,
            now=at,
            zone=timezone_for_country(country_for_phone(buyer_phone)),
        ),
    }
    if not all(values.values()):
        return CartDiscountStop(reason=REASON_PARAMETERS_MISSING, retryable=False)
    ordered = {
        str(position): values[name]
        for position, name in enumerate(CART_LAST_TOUCH_VARIABLES, start=1)
    }
    try:
        approved = parse_approved_template(
            inbox,
            template_name=template_name,
            expected_language=config.language,
            expected_category=config.category,
            parameter_count=len(CART_LAST_TOUCH_VARIABLES),
            checkout_button=True,
        )
        message = approved.render(ordered)
    except ApprovedTemplateError as exc:
        return CartDiscountStop(
            reason=exc.reason,
            retryable=exc.reason != REASON_PARAMETERS_MISSING,
        )
    reservation = await supabase.reserve_recovery_checkout_issuance(  # type: ignore[attr-defined]
        action_id=action.action_id,  # type: ignore[attr-defined]
        worker_id=worker_id,
        lease_generation=action.lease_generation,  # type: ignore[attr-defined]
        attempt_id=attempt.attempt_id,  # type: ignore[attr-defined]
        issuance_ulid=ulid_factory(),
        now=now,
    )
    if reservation.outcome != "reserved":
        # Z promises the discount in the button: without our link it does not
        # go out, whatever the reason.
        return CartDiscountStop(
            reason=f"recovery_checkout_{reservation.outcome}", retryable=False
        )
    assert reservation.issuance_id is not None
    assert reservation.checkout_url_final is not None
    try:
        suffix = followup_button_suffix(reservation.checkout_url_final, coupon.code)
        processed = approved.processed_params(ordered, button_suffix=suffix)
    except (ValueError, ApprovedTemplateError) as exc:
        return CartDiscountStop(
            reason=str(exc) or "cart_last_touch_button_invalid", retryable=False
        )
    return CartFollowupReady(
        message=message,
        template_name=template_name,
        template_params={
            "name": template_name,
            "category": config.category,
            "language": config.language,
            "processed_params": processed,
        },
        issuance_id=reservation.issuance_id,
    )


def is_cart_renewal_request(text: object) -> bool:
    """Whether an inbound message is exactly the «Quiero mi 10 %» reply of A2.

    Case, accents, surrounding quotes, repeated spaces and the space before
    the percent sign do not matter; any other word does. A sentence that only
    contains it is not the button.
    """
    if not isinstance(text, str) or len(text) > 200:
        return False
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = " ".join(folded.split()).strip(_QUOTES + " .!¡")
    return _RENEWAL_REPLY.fullmatch(folded) is not None
