"""B1 of ATT1: the first touch of a refused payment, in one of four variants.

Decided by Dan on 2026-10-18 (the flows of ATT1, flow 1, and the decision «el
pago rechazado le gana al primer contacto y B1-banco sale sin guia»): 15
minutes after the last refusal, one message per person, with the template of
the first variant that applies, in this order:

1. ``coupon``: the person has a valid coupon of another case of the product
   (the cart one, for example). It goes with that coupon and its expiry:
   taking it away right when they tried to pay would punish them for trying.
2. ``two_refusals``: two refusals or more, for any reason. No coupon.
3. ``insufficient_funds``: no balance or no limit. The only case where the
   discount attacks the cause: it goes with its own coupon of 24 hours,
   created by the coupon worker when the refusal is planned.
4. ``declined``: the bank did not approve, and any card reason we do not
   recognize. No coupon.

The refusal kind (``insufficient_funds`` or ``declined``) is the rule of the
base (``payment_failure_refusal_kind``, migration 20261019000200), read from
the send context: the bridge does not repeat it. Every variant carries the
checkout button with OUR issued link (and ``&offDiscount=<code>`` when it has
a coupon).

As in A1, B1 is never lost because of the coupon or the link: when its coupon
is not ``valid`` in time, or the link cannot be issued, the template of today
goes out. This module only decides and builds; the dispatcher (``worker.py``)
owns the attempt, the final gate and the send.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bridge.approved_templates import ApprovedTemplateError, parse_approved_template
from bridge.cart_discount import country_for_phone, format_moment, timezone_for_country
from bridge.cart_first_touch import (
    RECOVERY_COUPON_NOT_READY,
    CartDiscountDefer,
    CartDiscountFallback,
    CartDiscountOutcome,
    CartDiscountReady,
    CartDiscountStop,
)
from bridge.followup_discount import HOTMART_CHECKOUT_PREFIX, followup_button_suffix
from bridge.supabase import PilotBoundaryConfig

logger = logging.getLogger(__name__)

PAYMENT_FAILURE_TRIGGER = "payment_failure"

VARIANT_COUPON = "coupon"
VARIANT_TWO_REFUSALS = "two_refusals"
VARIANT_INSUFFICIENT_FUNDS = "insufficient_funds"
VARIANT_DECLINED = "declined"
VARIANTS = (
    VARIANT_COUPON,
    VARIANT_TWO_REFUSALS,
    VARIANT_INSUFFICIENT_FUNDS,
    VARIANT_DECLINED,
)

# The order of the variables of each approved template (estado.json of the
# templates of ATT1, uploaded on 2026-10-18).
VARIANT_VARIABLES: Mapping[str, tuple[str, ...]] = {
    VARIANT_COUPON: ("nombre", "cuando", "producto", "vence"),
    VARIANT_TWO_REFUSALS: ("nombre", "cuando", "producto"),
    VARIANT_INSUFFICIENT_FUNDS: ("nombre", "cuando", "producto", "vence"),
    VARIANT_DECLINED: ("nombre", "cuando", "producto"),
}
DEFAULT_VARIANT_CATEGORIES: Mapping[str, str] = {
    VARIANT_COUPON: "MARKETING",
    VARIANT_TWO_REFUSALS: "UTILITY",
    VARIANT_INSUFFICIENT_FUNDS: "MARKETING",
    VARIANT_DECLINED: "UTILITY",
}

# A coupon in these states will not become valid by waiting.
_COUPON_DEAD = frozenset({"create_failed", "expired", "redeemed"})
_STOP_OUTCOMES = frozenset({
    "purchase_already_approved", "blocked_case", "blocked_contact",
    "blocked_opt_out", "blocked_intent",
})
_REPLAY_OUTCOMES = frozenset({
    "request_started_replay", "already_accepted", "delivery_unknown",
})


@dataclass(frozen=True)
class PaymentFailurePlanningConfig:
    """What the resolution worker needs to plan a refusal: its own scope and policy.

    The refusal is not planned under the policy shared with the cart (its
    grace is 15 minutes) and a published scope carries a single policy.
    """

    policy_key: str
    policy_version: int
    pilot_boundary: PilotBoundaryConfig

    def __post_init__(self) -> None:
        if not self.policy_key.strip() or self.policy_version < 1:
            raise ValueError("payment_failure_policy_invalid")


@dataclass(frozen=True)
class PaymentFailureVariantsConfig:
    templates: Mapping[str, str]
    language: str
    categories: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_VARIANT_CATEGORIES)
    )
    defer_delay: timedelta = timedelta(minutes=5)
    max_defer: timedelta = timedelta(minutes=15)
    defer_attempts: int = 2

    def __post_init__(self) -> None:
        if set(self.templates) != set(VARIANTS) or any(
            not isinstance(name, str) or not name.strip()
            for name in self.templates.values()
        ):
            raise ValueError("payment_failure_templates_invalid")
        if len(set(self.templates.values())) != len(VARIANTS):
            raise ValueError("payment_failure_templates_not_distinct")
        if not self.language.strip():
            raise ValueError("payment_failure_language_invalid")
        if set(self.categories) != set(VARIANTS) or any(
            category not in {"MARKETING", "UTILITY"}
            for category in self.categories.values()
        ):
            raise ValueError("payment_failure_categories_invalid")
        if self.defer_attempts < 0 or self.defer_delay <= timedelta(0):
            raise ValueError("payment_failure_defer_invalid")


def choose_variant(
    *,
    refusal_kind: str,
    payment_failure_count: int,
    other_coupon_code: str | None,
) -> str:
    """The first variant that applies, in the order decided on 2026-10-17."""
    if refusal_kind not in {"declined", "insufficient_funds"}:
        # not_a_refusal is never planned (the base refuses it); anything else
        # is a contract break, not a reason to pick a text.
        raise ValueError("payment_failure_refusal_kind_invalid")
    if payment_failure_count < 1:
        raise ValueError("payment_failure_count_invalid")
    if other_coupon_code is not None:
        return VARIANT_COUPON
    if payment_failure_count >= 2:
        return VARIANT_TWO_REFUSALS
    if refusal_kind == "insufficient_funds":
        return VARIANT_INSUFFICIENT_FUNDS
    return VARIANT_DECLINED


def recovery_button_suffix(checkout_url_final: str, coupon_code: str | None) -> str:
    """The variable part of the button: our issued link, with the coupon if any."""
    if coupon_code is not None:
        return followup_button_suffix(checkout_url_final, coupon_code)
    if (
        not isinstance(checkout_url_final, str)
        or not checkout_url_final.startswith(HOTMART_CHECKOUT_PREFIX)
        or len(checkout_url_final) == len(HOTMART_CHECKOUT_PREFIX)
    ):
        raise ValueError("payment_failure_checkout_url_invalid")
    return checkout_url_final[len(HOTMART_CHECKOUT_PREFIX):]


def payment_failure_values(
    *,
    variant: str,
    buyer_name: str,
    product_name: str,
    failed_at: datetime,
    coupon_expires_at: datetime | None,
    country: str | None,
    now: datetime,
) -> dict[str, str]:
    """The variables of the variant by manifest name, whitespace collapsed."""
    zone = timezone_for_country(country)
    values = {
        "nombre": buyer_name or "",
        "producto": product_name or "",
        "cuando": format_moment(failed_at, now=now, zone=zone),
    }
    if "vence" in VARIANT_VARIABLES[variant]:
        if coupon_expires_at is None or coupon_expires_at <= now:
            raise ValueError("payment_failure_coupon_already_expired")
        values["vence"] = format_moment(coupon_expires_at, now=now, zone=zone)
    return {
        name: " ".join(values[name].split()) for name in VARIANT_VARIABLES[variant]
    }


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("payment_failure_naive_datetime")
    return parsed


async def compose_payment_failure_first_touch(
    *,
    config: PaymentFailureVariantsConfig,
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
    """Decide B1 for one refusal action and build it."""
    at = _aware(now)
    context = await supabase.get_portable_payment_failure_send_context(  # type: ignore[attr-defined]
        action_id=action.action_id,  # type: ignore[attr-defined]
        worker_id=worker_id,
        lease_generation=action.lease_generation,  # type: ignore[attr-defined]
        now=now,
    )
    try:
        variant = choose_variant(
            refusal_kind=context.refusal_kind,
            payment_failure_count=context.payment_failure_count,
            other_coupon_code=context.other_coupon_code,
        )
    except ValueError as exc:
        return CartDiscountFallback(str(exc))
    coupon_code: str | None = None
    coupon_expires_at: datetime | None = None
    if variant == VARIANT_COUPON:
        coupon_code = context.other_coupon_code
        coupon_expires_at = context.other_coupon_expires_at
    elif variant == VARIANT_INSUFFICIENT_FUNDS:
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
                return CartDiscountDefer(
                    retry_after=config.defer_delay, reason=RECOVERY_COUPON_NOT_READY
                )
            return CartDiscountFallback("recovery_coupon_not_ready_in_time")
        coupon_code = coupon.code
        coupon_expires_at = coupon.expires_at
    template_name = config.templates[variant]
    category = config.categories[variant]
    variables = VARIANT_VARIABLES[variant]
    try:
        values = payment_failure_values(
            variant=variant,
            buyer_name=buyer_name,
            product_name=product_name,
            failed_at=context.latest_failed_at,
            coupon_expires_at=coupon_expires_at,
            country=country_for_phone(buyer_phone),
            now=at,
        )
        ordered = {
            str(position): values[name]
            for position, name in enumerate(variables, start=1)
        }
        approved = parse_approved_template(
            inbox,
            template_name=template_name,
            expected_language=config.language,
            expected_category=category,
            parameter_count=len(variables),
            checkout_button=True,
        )
        message = approved.render(ordered)
    except ApprovedTemplateError as exc:
        return CartDiscountFallback(f"payment_failure_template_{exc.reason}")
    except ValueError as exc:
        return CartDiscountFallback(str(exc) or "payment_failure_values_invalid")
    reservation = await supabase.reserve_recovery_checkout_issuance(  # type: ignore[attr-defined]
        action_id=action.action_id,  # type: ignore[attr-defined]
        worker_id=worker_id,
        lease_generation=action.lease_generation,  # type: ignore[attr-defined]
        attempt_id=attempt.attempt_id,  # type: ignore[attr-defined]
        issuance_ulid=ulid_factory(),
        now=now,
    )
    if reservation.outcome in _STOP_OUTCOMES or reservation.outcome in _REPLAY_OUTCOMES:
        return CartDiscountStop(
            reason=f"recovery_checkout_{reservation.outcome}", retryable=False
        )
    if reservation.outcome != "reserved":
        return CartDiscountFallback(f"recovery_checkout_{reservation.outcome}")
    assert reservation.issuance_id is not None
    assert reservation.checkout_url_final is not None
    try:
        suffix = recovery_button_suffix(reservation.checkout_url_final, coupon_code)
        processed = approved.processed_params(ordered, button_suffix=suffix)
    except (ValueError, ApprovedTemplateError) as exc:
        return CartDiscountFallback(str(exc) or "payment_failure_button_invalid")
    logger.info(
        "payment_failure_first_touch_variant action_id=%s variant=%s",
        action.action_id,  # type: ignore[attr-defined]
        variant,
    )
    return CartDiscountReady(
        message=message,
        template_name=template_name,
        template_params={
            "name": template_name,
            "category": category,
            "language": config.language,
            "processed_params": processed,
        },
        issuance_id=reservation.issuance_id,
    )
