"""C2 and C3 of the ATT1 flows: the reminders of a payment slip's due date.

Contract: ``docs/contracts/portable-payment-slip-reminders-v1.md``.
Specified in «los flujos comerciales de ATT1 como tienen que quedar», 2b
(2026-10-17): after C1, the slip gets a reminder the day before it expires
(C2, ``att1_ficha_vence_manana_01``) and another one on the due date itself
(C3, ``att1_ficha_vence_hoy_01``), each with its approved template and the
slip in the URL button.

The steps live in the policy of the slip scope, after
``payment_slip_first_contact``. The engine inserts each one when the previous
one is accepted (``_finalize_followup_delivery_attempt``), as a
``no_reply_review`` with a short delay counted from C1. Its moment is anchored
to the due date, which only the bridge computes (``bridge.payment_slip``): when
the step is composed before its moment, the attempt is deferred once until
then; when its day already went by, the step does not go out.

This module only names the steps and decides the moment; the composition is
``payment_slip_first_touch.compose_payment_slip_first_touch`` with ``step``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bridge.messaging import PAYMENT_SLIP_TRIGGER

__all__ = [
    "FOLLOWING_STEP_TRIGGERS",
    "PAYMENT_SLIP_DUE_DAY_STEP",
    "PAYMENT_SLIP_DUE_EVE_STEP",
    "PAYMENT_SLIP_STEPS",
    "PAYMENT_SLIP_STEP_LATE",
    "PAYMENT_SLIP_STEP_NOT_DUE",
    "PaymentSlipStep",
    "payment_slip_step_wait",
]

# The step keys of the slip policy after C1 (publicar-ficha-v2.sql of the
# instance writes them in the same order).
PAYMENT_SLIP_DUE_EVE_STEP = "payment_slip_due_eve"
PAYMENT_SLIP_DUE_DAY_STEP = "payment_slip_due_day"

# The step was composed before its moment: the attempt waits until then.
PAYMENT_SLIP_STEP_NOT_DUE = "payment_slip_step_not_due"
# The day of the step already went by in the zone of the country (C1 went out
# late): a reminder of «vence mañana» on another day says something false.
PAYMENT_SLIP_STEP_LATE = "payment_slip_step_late"


@dataclass(frozen=True)
class PaymentSlipStep:
    """One reminder: its manifest slot and its moment relative to the due date."""

    step_key: str
    slot: str
    days_before_due: int
    local_hour: int


PAYMENT_SLIP_STEPS: dict[str, PaymentSlipStep] = {
    # C2: the day before the due date, in the morning, so the person still has
    # that day and the next one to pay.
    PAYMENT_SLIP_DUE_EVE_STEP: PaymentSlipStep(
        step_key=PAYMENT_SLIP_DUE_EVE_STEP,
        slot="ficha_vence_manana",
        days_before_due=1,
        local_hour=10,
    ),
    # C3: the due date at 09:00 of the person (the specification).
    PAYMENT_SLIP_DUE_DAY_STEP: PaymentSlipStep(
        step_key=PAYMENT_SLIP_DUE_DAY_STEP,
        slot="ficha_vence_hoy",
        days_before_due=0,
        local_hour=9,
    ),
}

# The flow a following step belongs to, by its step key. A ``no_reply_review``
# is anchored to the accepted message of the step before it, so its anchor
# does not say which flow it is: the dispatcher reads it from here. A first
# contact keeps deciding by its anchor.
FOLLOWING_STEP_TRIGGERS: dict[str, str] = {
    step_key: PAYMENT_SLIP_TRIGGER for step_key in PAYMENT_SLIP_STEPS
}


def payment_slip_step_wait(
    step: PaymentSlipStep, *, due_date: date, zone: str, now: datetime
) -> timedelta | str | None:
    """How long the step waits, ``PAYMENT_SLIP_STEP_LATE``, or ``None`` to send now.

    The day of the step is ``due_date - days_before_due`` in ``zone``. Before
    ``local_hour`` of that day it waits; on that day from then on it goes out;
    on a later day it is late.
    """
    if now.tzinfo is None:
        raise ValueError("payment_slip_naive_datetime")
    tz = ZoneInfo(zone)
    day = due_date - timedelta(days=step.days_before_due)
    if now.astimezone(tz).date() > day:
        return PAYMENT_SLIP_STEP_LATE
    moment = datetime.combine(day, time(step.local_hour), tzinfo=tz)
    if now < moment:
        return moment - now
    return None
