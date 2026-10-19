"""C1 of the ATT1 flows: the first contact one hour after a payment slip.

Contract: ``docs/contracts/portable-payment-slip-first-contact-v1.md``.
Decided by Dan on 2026-10-18 (decision «la ficha de OXXO va con scope propio y
código global», points 4 and 5): the slip goes out with its approved template
(``att1_ficha_efectivo_01``): the name, the product, the store and the due
date in the body, and the slip itself in a URL button. A slip that already
expired in the time zone of its country when C1 is composed is not sent
(``payment_slip_expired``).

The base keeps the raw facts of the slip; the store, the due date and the
button suffix are computed here, at composition time, from the row of
``[[ficha.medios]]`` (``bridge.payment_slip``). Whatever does not resolve
(a method or country not enabled, a link on another base, a suffix that is not
safe, a due date nobody can tell) closes the attempt without a message: none
of them changes by retrying.

This module only decides and builds; the dispatcher (``worker.py``) owns the
attempt, the final gate and the send.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from bridge.approved_templates import (
    REASON_MISMATCH,
    REASON_PARAMETERS_MISSING,
    ApprovedTemplateError,
    parse_approved_template,
)
from bridge.messaging import PAYMENT_SLIP_TRIGGER
from bridge.payment_slip import (
    PaymentSlipFacts,
    PaymentSlipMethod,
    format_due_date_es,
    resolve_payment_slip,
)
from bridge.supabase import PilotBoundaryConfig, SupabaseError, SupabasePermanentError

__all__ = [
    "PAYMENT_SLIP_CONTEXT_UNAVAILABLE",
    "PAYMENT_SLIP_EXPIRED",
    "PAYMENT_SLIP_TRIGGER",
    "PaymentSlipFirstTouchConfig",
    "PaymentSlipPlanningConfig",
    "PaymentSlipReady",
    "PaymentSlipStop",
    "compose_payment_slip_first_touch",
    "payment_slip_ingestion_reason",
]

# Decision 5 of 2026-10-18: the due date already passed in the zone of the
# country when C1 is composed. On the due date itself the slip can still be
# paid («hasta el jueves 22»), so only a later day counts.
PAYMENT_SLIP_EXPIRED = "payment_slip_expired"
# The facts of the slip could not be read under the lease (the base did not
# answer). Retryable: the next lease reads them again.
PAYMENT_SLIP_CONTEXT_UNAVAILABLE = "payment_slip_context_unavailable"


@dataclass(frozen=True)
class PaymentSlipFirstTouchConfig:
    """The ``[[ficha.medios]]`` rows of the instance.

    The template (name, category, variables) is the one of
    ``WhatsAppTemplateConfig.payment_slip_*``, like every other first contact.
    """

    methods: tuple[PaymentSlipMethod, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.methods, tuple) or not all(
            isinstance(method, PaymentSlipMethod) for method in self.methods
        ):
            raise ValueError("payment_slip_methods_invalid")
        if not any(method.habilitada for method in self.methods):
            # Nothing would ever go out: the flag on without a row is a
            # configuration error, not a quiet runtime.
            raise ValueError("payment_slip_methods_none_enabled")


@dataclass(frozen=True)
class PaymentSlipPlanningConfig:
    """What the resolution worker needs to plan a slip: its own scope and policy.

    The slip is not planned under the shared recovery policy (its grace is one
    hour and it expires in a day) nor under the recovery scope (a published
    scope has a single main event type).
    """

    methods: tuple[PaymentSlipMethod, ...]
    policy_key: str
    policy_version: int
    pilot_boundary: PilotBoundaryConfig

    def __post_init__(self) -> None:
        PaymentSlipFirstTouchConfig(methods=self.methods)
        if not self.policy_key.strip() or self.policy_version < 1:
            raise ValueError("payment_slip_policy_invalid")


@dataclass(frozen=True)
class PaymentSlipReady:
    message: str
    template_name: str
    template_params: dict[str, object]
    due_date_source: str


@dataclass(frozen=True)
class PaymentSlipStop:
    """C1 does not go out: the attempt closes with ``reason``."""

    reason: str
    retryable: bool


PaymentSlipOutcome = PaymentSlipReady | PaymentSlipStop


def payment_slip_ingestion_reason(
    facts: PaymentSlipFacts, methods: tuple[PaymentSlipMethod, ...]
) -> str | None:
    """The reason a slip is not planned at all, or ``None`` to plan it.

    The same resolution the composition uses, at ingestion: a method or
    country without an enabled row, a link on another base, a suffix that is
    not safe or a due date nobody can tell is ``ignored`` with its reason, and
    nobody is written to. The expiry is not checked here: it depends on when
    C1 is composed.
    """
    resolution = resolve_payment_slip(facts, methods)
    return resolution if isinstance(resolution, str) else None


def _collapse(value: str) -> str:
    # Meta refuses a body parameter with a new line, a tab or more than four
    # spaces; the other first contacts collapse the same way (messaging.
    # WhatsAppTemplateConfig.body_values).
    return " ".join(value.split())


async def compose_payment_slip_first_touch(
    *,
    config: PaymentSlipFirstTouchConfig,
    supabase: object,
    inbox: Mapping[str, object],
    action: object,
    template_name: str,
    language: str,
    category: str,
    parameters: tuple[str, ...],
    buyer_name: str,
    product_name: str,
    worker_id: str,
    now: str,
) -> PaymentSlipOutcome:
    """Build C1 for one payment slip action, or say why it does not go out."""
    try:
        context = await supabase.get_portable_payment_slip_send_context(  # type: ignore[attr-defined]
            action_id=action.action_id,  # type: ignore[attr-defined]
            worker_id=worker_id,
            lease_generation=action.lease_generation,  # type: ignore[attr-defined]
            now=now,
        )
    except SupabasePermanentError as exc:
        return PaymentSlipStop(
            reason=exc.reason or PAYMENT_SLIP_CONTEXT_UNAVAILABLE, retryable=False
        )
    except SupabaseError:
        return PaymentSlipStop(reason=PAYMENT_SLIP_CONTEXT_UNAVAILABLE, retryable=True)

    facts = PaymentSlipFacts(
        payment_type=context.payment_type,
        checkout_country_iso=context.checkout_country_iso,
        billet_url=context.billet_url,
        billet_barcode=context.billet_barcode,
        generated_at=context.generated_at,
    )
    resolution = resolve_payment_slip(facts, config.methods)
    if isinstance(resolution, str):
        # The manifest changed since the slip was planned, or the base keeps a
        # slip the ingestion would not have planned: nothing goes out.
        return PaymentSlipStop(reason=resolution, retryable=False)

    at = datetime.fromisoformat(now)
    if at.tzinfo is None:
        raise ValueError("payment_slip_naive_datetime")
    today = at.astimezone(ZoneInfo(resolution.method.zona_horaria)).date()
    if today > resolution.due_date:
        return PaymentSlipStop(reason=PAYMENT_SLIP_EXPIRED, retryable=False)

    source = {
        "nombre": _collapse(buyer_name),
        "producto": _collapse(product_name),
        "tienda": _collapse(resolution.tienda),
        "vencimiento": format_due_date_es(resolution.due_date),
    }
    if not parameters or any(item not in source for item in parameters):
        return PaymentSlipStop(reason=REASON_MISMATCH, retryable=False)
    values = {
        str(position): source[parameter]
        for position, parameter in enumerate(parameters, start=1)
    }
    try:
        approved = parse_approved_template(
            inbox,
            template_name=template_name,
            expected_language=language,
            expected_category=category,
            parameter_count=len(parameters),
            url_button=resolution.button_url_template,
        )
        message = approved.render(values)
        processed = approved.processed_params(
            values, button_suffix=resolution.button_suffix
        )
    except ApprovedTemplateError as exc:
        # As the other first contacts: a value of the case does not change on
        # its own, a catalog that does not close can.
        return PaymentSlipStop(
            reason=exc.reason, retryable=exc.reason != REASON_PARAMETERS_MISSING
        )
    return PaymentSlipReady(
        message=message,
        template_name=template_name,
        template_params={
            "name": template_name,
            "category": category,
            "language": language,
            "processed_params": processed,
        },
        due_date_source=resolution.due_date_source,
    )
