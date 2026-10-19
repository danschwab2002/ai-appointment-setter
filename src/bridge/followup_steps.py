"""The flow each following step belongs to, for every flow with more than one touch.

A ``no_reply_review`` is anchored to the accepted message of the step before
it, so its anchor does not say which flow it is: the dispatcher reads it from
here, by step key. A first contact keeps deciding by its anchor, and a step
key nobody declared keeps the anchor too, which is what every following step
did before.

Each flow names its own steps next to its composition
(``payment_slip_reminders``, ``cart_followups``,
``payment_failure_followups``); this module only joins them, so the
dispatcher does not depend on one flow to know the others.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from bridge.cart_followups import CART_STEP_TRIGGERS
from bridge.payment_failure_followups import PAYMENT_FAILURE_STEP_TRIGGERS
from bridge.payment_slip_reminders import (
    FOLLOWING_STEP_TRIGGERS as PAYMENT_SLIP_STEP_TRIGGERS,
)

__all__ = ["FOLLOWING_STEP_TRIGGERS"]


def _join(*maps: Mapping[str, str]) -> Mapping[str, str]:
    joined: dict[str, str] = {}
    for one in maps:
        for step_key, trigger in one.items():
            if step_key in joined:
                raise ValueError(f"duplicate_following_step:{step_key}")
            joined[step_key] = trigger
    return MappingProxyType(joined)


FOLLOWING_STEP_TRIGGERS: Mapping[str, str] = _join(
    PAYMENT_SLIP_STEP_TRIGGERS, CART_STEP_TRIGGERS, PAYMENT_FAILURE_STEP_TRIGGERS
)
