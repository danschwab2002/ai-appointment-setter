"""The follow-up of a refused payment in ATT1: B2 or Z, the second and last touch.

Contract: ``docs/contracts/portable-payment-failure-followup-v1.md``.
Decided by Dan on 2026-10-19: to whoever did not answer, the refused payment
sends at most two messages, B1 and ONE follow-up 24 hours after it:

- B2 (``second_touch``, ``att1_pago_rechazado_descuento_01``) when B1 went
  out without a coupon (two refusals, the bank, or the template of today);
- Z (``last_touch``, ``att1_ultima_renovacion_01``, the same Z as the cart)
  when B1 went out with one (the coupon of another case, or the one of
  insufficient funds).

The base decides which one (``get_payment_failure_followup_context_v1``, from
the variant B1 recorded). Both carry coupon 2 of the case and our link in the
checkout button: for B2 it is the first code of the person, for Z the
renewal. The build is the one of Z of the cart (``compose_cart_last_touch``):
it waits for the coupon, and a follow-up never promises a discount without
its valid coupon behind it, so without the coupon in time, or without our
link, it closes without a message.

The step lives in the refused payment policy of the instance after the first
contact; the engine inserts it when B1 is accepted. This module only decides
and builds; the dispatcher (``worker.py``) owns the attempt, the final gate
and the send.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from bridge.cart_first_touch import CartDiscountStop
from bridge.cart_followups import (
    CART_LAST_TOUCH_COUPON_NOT_READY_IN_TIME,
    CART_LAST_TOUCH_VARIABLES,
    CartFollowupConfig,
    CartFollowupOutcome,
    compose_cart_last_touch,
)
from bridge.payment_failure_variant import PAYMENT_FAILURE_TRIGGER

logger = logging.getLogger(__name__)

__all__ = [
    "FOLLOWUP_KIND_LAST_TOUCH",
    "FOLLOWUP_KIND_SECOND_TOUCH",
    "PAYMENT_FAILURE_FOLLOWUP_STEP",
    "PAYMENT_FAILURE_FOLLOWUP_VARIABLES",
    "PAYMENT_FAILURE_STEP_TRIGGERS",
    "PaymentFailureFollowupConfig",
    "compose_payment_failure_followup",
]

# The step key of the refused payment policy after the first contact.
PAYMENT_FAILURE_FOLLOWUP_STEP = "payment_failure_followup"
PAYMENT_FAILURE_STEP_TRIGGERS: Mapping[str, str] = {
    PAYMENT_FAILURE_FOLLOWUP_STEP: PAYMENT_FAILURE_TRIGGER,
}

FOLLOWUP_KIND_SECOND_TOUCH = "second_touch"
FOLLOWUP_KIND_LAST_TOUCH = "last_touch"
# B2 and Z take the same variables, in this order (the approved templates of
# ATT1, uploaded on 2026-10-19).
PAYMENT_FAILURE_FOLLOWUP_VARIABLES = CART_LAST_TOUCH_VARIABLES

PAYMENT_FAILURE_FOLLOWUP_COUPON_NOT_READY_IN_TIME = (
    "payment_failure_followup_coupon_not_ready_in_time"
)


@dataclass(frozen=True)
class PaymentFailureFollowupConfig:
    """The approved templates of B2 and Z. Both are required: the base picks."""

    language: str
    second_touch_template: str
    last_touch_template: str
    category: str = "MARKETING"

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise ValueError("payment_failure_followup_language_invalid")
        for name in (self.second_touch_template, self.last_touch_template):
            if not isinstance(name, str) or not name.strip():
                raise ValueError("payment_failure_followup_template_invalid")
        if self.second_touch_template == self.last_touch_template:
            raise ValueError("payment_failure_followup_templates_not_distinct")
        if self.category not in {"MARKETING", "UTILITY"}:
            raise ValueError("payment_failure_followup_category_invalid")

    def template_for(self, followup_kind: str) -> str | None:
        if followup_kind == FOLLOWUP_KIND_SECOND_TOUCH:
            return self.second_touch_template
        if followup_kind == FOLLOWUP_KIND_LAST_TOUCH:
            return self.last_touch_template
        return None


async def compose_payment_failure_followup(
    *,
    config: PaymentFailureFollowupConfig,
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
    """B2 or Z, with coupon 2 and our link in the button, or no message at all.

    The context of the base is read first: without it (an error of the base)
    the caller fails the attempt like any lookup.
    """
    context = await supabase.get_payment_failure_followup_context(  # type: ignore[attr-defined]
        action_id=action.action_id,  # type: ignore[attr-defined]
        worker_id=worker_id,
        lease_generation=action.lease_generation,  # type: ignore[attr-defined]
        now=now,
    )
    template_name = config.template_for(context.followup_kind)
    if template_name is None:
        return CartDiscountStop(
            reason="payment_failure_followup_kind_invalid", retryable=False
        )
    logger.info(
        "payment_failure_followup_kind action_id=%s kind=%s first_touch_variant=%s",
        action.action_id,  # type: ignore[attr-defined]
        context.followup_kind,
        context.first_touch_variant,
    )
    outcome = await compose_cart_last_touch(
        config=CartFollowupConfig(
            language=config.language,
            last_touch_template=template_name,
            category=config.category,
        ),
        supabase=supabase,
        inbox=inbox,
        action=action,
        attempt=attempt,
        buyer_name=buyer_name,
        product_name=product_name,
        buyer_phone=buyer_phone,
        worker_id=worker_id,
        now=now,
        ulid_factory=ulid_factory,
    )
    if (
        isinstance(outcome, CartDiscountStop)
        and outcome.reason == CART_LAST_TOUCH_COUPON_NOT_READY_IN_TIME
    ):
        # The same stop, named after this flow in the attempt and the logs.
        return CartDiscountStop(
            reason=PAYMENT_FAILURE_FOLLOWUP_COUPON_NOT_READY_IN_TIME,
            retryable=outcome.retryable,
        )
    return outcome
