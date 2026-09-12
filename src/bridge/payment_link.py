"""Deterministic payment-link construction from an admitted precheckout URL."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit


_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_PREFIX = re.compile(r"[a-z0-9][a-z0-9-]{0,30}-")
_SCHEMELESS_URL = re.compile(
    r"(?<![@\w])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?=[:/?#\s.,;!?)\]]|$)",
    re.IGNORECASE,
)


class PaymentLinkUnavailable(ValueError):
    """The canonical checkout cannot safely produce a payment link."""


@dataclass(frozen=True, slots=True)
class PaymentLinkConfig:
    tracking_fields: tuple[str, ...] = ("src", "xcod")
    tracking_prefix: str = "hermes-"
    max_age_seconds: int = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if self.tracking_fields != ("src", "xcod"):
            raise ValueError("tracking_fields must be exactly ('src', 'xcod')")
        if _PREFIX.fullmatch(self.tracking_prefix) is None:
            raise ValueError("tracking_prefix must be lowercase alphanumeric/hyphen and end in hyphen")
        if type(self.max_age_seconds) is not int or self.max_age_seconds < 1:
            raise ValueError("max_age_seconds must be a positive integer")


@dataclass(frozen=True, slots=True)
class PaymentLink:
    final_url: str
    tracking_field: str
    tracking_value: str


def build_payment_link(
    *,
    checkout_url: str,
    sequence_origin_event_id: str,
    submitted_at: datetime,
    now: datetime,
    config: PaymentLinkConfig,
) -> PaymentLink:
    """Append one tracking key while preserving every existing URL byte."""

    if _ULID.fullmatch(sequence_origin_event_id) is None:
        raise PaymentLinkUnavailable("invalid_sequence_origin_event_id")
    if submitted_at.tzinfo is None or now.tzinfo is None:
        raise PaymentLinkUnavailable("invalid_checkout_timestamp")
    age_seconds = (now - submitted_at).total_seconds()
    if age_seconds < 0:
        raise PaymentLinkUnavailable("checkout_url_from_future")
    if age_seconds > config.max_age_seconds:
        raise PaymentLinkUnavailable("checkout_url_stale")

    if not isinstance(checkout_url, str) or any(
        character.isspace() or not character.isprintable()
        for character in checkout_url
    ):
        raise PaymentLinkUnavailable("invalid_checkout_url")

    try:
        parsed = urlsplit(checkout_url)
        existing_fields = {key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    except (TypeError, ValueError) as exc:
        raise PaymentLinkUnavailable("invalid_checkout_url") from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc != "pay.hotmart.com"
        or not parsed.path.startswith("/")
        or not parsed.query
        or parsed.fragment
        or "?" not in checkout_url
    ):
        raise PaymentLinkUnavailable("invalid_checkout_url")

    tracking_field = next(
        (field for field in config.tracking_fields if field not in existing_fields),
        None,
    )
    if tracking_field is None:
        raise PaymentLinkUnavailable("tracking_fields_occupied")

    tracking_value = f"{config.tracking_prefix}{sequence_origin_event_id}"
    return PaymentLink(
        final_url=f"{checkout_url}&{tracking_field}={tracking_value}",
        tracking_field=tracking_field,
        tracking_value=tracking_value,
    )


def render_payment_link_reply(*, preamble: str, final_url: str) -> str:
    """Attach the bridge-owned URL to agent-authored text without reinterpretation."""

    cleaned = preamble.rstrip()
    if not cleaned:
        raise PaymentLinkUnavailable("agent_reply_empty")
    if any(marker in cleaned.lower() for marker in ("http://", "https://", "www.")):
        raise PaymentLinkUnavailable("agent_reply_contains_url")
    if _SCHEMELESS_URL.search(cleaned) is not None:
        raise PaymentLinkUnavailable("agent_reply_contains_url")
    if not isinstance(final_url, str) or not final_url.startswith(
        "https://pay.hotmart.com/"
    ):
        raise PaymentLinkUnavailable("invalid_checkout_url")
    return f"{cleaned}\n{final_url}"
