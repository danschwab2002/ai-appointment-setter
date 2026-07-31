"""Hotmart webhook validation and event classification.

Hotmart does not use HMAC signatures.  Instead, every account has a single
secret token (``hottok``) that Hotmart sends in the ``X-HOTMART-HOTTOK``
header on every request.  Validation is a constant-time string comparison,
fail-closed.
"""

from __future__ import annotations

import hmac
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

# ── Event constants (v2.0.0) ─────────────────────────────────────────

EVENT_CART_ABANDONMENT = "PURCHASE_OUT_OF_SHOPPING_CART"
EVENT_VERSION = "2.0.0"


# ── Parsed buyer data ────────────────────────────────────────────────


@dataclass(frozen=True)
class HotmartBuyerData:
    """Normalised data extracted from a Hotmart cart-abandonment payload."""

    event_id: str
    event_type: str
    creation_date_ms: int
    buyer_name: str | None
    buyer_email: str | None
    buyer_phone: str | None
    product_id: int | None
    product_name: str | None
    offer_code: str | None
    checkout_country_iso: str | None
    checkout_country_name: str | None
    affiliate: bool | None


# ── Normalisation helpers ────────────────────────────────────────────

# Strip everything that is not a digit.  Hotmart sends phone with DDI
# and no "+": "5531999999999".  We store the raw digits as-is.
_NON_DIGIT = re.compile(r"\D")


def normalize_email(raw: str | None) -> str | None:
    """Lower-case and trim an email, returning ``None`` if empty."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def normalize_phone(raw: str | None) -> str | None:
    """Strip a phone number to bare digits, returning ``None`` if empty."""
    if not isinstance(raw, str):
        return None
    digits = _NON_DIGIT.sub("", raw)
    return digits or None


def _str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool(value: Any) -> bool | None:
    if not isinstance(value, bool):
        return None
    return value


def parse_hotmart_payload(payload: object) -> HotmartBuyerData | None:
    """Extract normalised buyer data from a Hotmart v2.0.0 payload.

    Returns ``None`` if the payload is structurally invalid.
    """
    event = _json_object(payload)
    data = _json_object(event.get("data"))
    buyer = _json_object(data.get("buyer"))
    product = _json_object(data.get("product"))
    offer = _json_object(data.get("offer"))
    country = _json_object(data.get("checkout_country"))

    event_id = _str(event.get("id"))
    if event_id is None:
        return None

    creation_date = event.get("creation_date")
    if isinstance(creation_date, bool) or not isinstance(creation_date, int):
        return None

    return HotmartBuyerData(
        event_id=event_id,
        event_type=_str(event.get("event")) or EVENT_CART_ABANDONMENT,
        creation_date_ms=creation_date,
        buyer_name=_str(buyer.get("name")),
        buyer_email=normalize_email(buyer.get("email")),
        buyer_phone=normalize_phone(buyer.get("phone")),
        product_id=_int(product.get("id")),
        product_name=_str(product.get("name")),
        offer_code=_str(offer.get("code")),
        checkout_country_iso=_str(country.get("iso")),
        checkout_country_name=_str(country.get("name")),
        affiliate=_bool(data.get("affiliate")),
    )

# ── Decision types ───────────────────────────────────────────────────

HotmartAction = Literal["persist", "ignore"]


@dataclass(frozen=True)
class HotmartDecision:
    accepted: bool
    reason: str
    action: HotmartAction
    event_id: str | None
    event_type: str | None


# ── Header validation ────────────────────────────────────────────────


def verify_hotmart_token(*, received_token: str, expected_token: str) -> bool:
    """Return whether the X-HOTMART-HOTTOK header matches the configured token."""
    return hmac.compare_digest(received_token, expected_token)


# ── Anti-replay ─────────────────────────────────────────────────────


def _creation_date_to_seconds(creation_date: Any) -> float | None:
    """Convert Hotmart's epoch-millis ``creation_date`` to seconds.

    Returns ``None`` if the value is not a valid number.
    """
    if isinstance(creation_date, bool) or not isinstance(creation_date, (int, float)):
        return None
    return creation_date / 1000.0


def is_stale_event(
    *, creation_date: Any, max_age_seconds: int, now: float | None = None
) -> bool | None:
    """Return ``True`` if the event is older than ``max_age_seconds``.

    Returns ``None`` if the ``creation_date`` cannot be parsed, signalling
    the caller to reject the event for a different reason.
    """
    event_seconds = _creation_date_to_seconds(creation_date)
    if event_seconds is None:
        return None
    current = now if now is not None else time.time()
    return abs(current - event_seconds) > max_age_seconds


# ── Payload classification ───────────────────────────────────────────


def _json_object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def classify_hotmart_event(payload: object) -> HotmartDecision:
    """Classify a Hotmart webhook payload before any persistence.

    The payload shape (v2.0.0)::

        {
          "id": "uuid-string",
          "creation_date": 1632411406874,
          "event": "PURCHASE_OUT_OF_SHOPPING_CART",
          "version": "2.0.0",
          "data": { ... }
        }
    """
    event = _json_object(payload)
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        return HotmartDecision(
            False, "missing_event_id", "ignore", None, None
        )

    event_type = event.get("event")
    if not isinstance(event_type, str) or event_type != EVENT_CART_ABANDONMENT:
        return HotmartDecision(
            False, "unsupported_event_type", "ignore", event_id, event_type
        )

    version = event.get("version")
    if not isinstance(version, str) or version != EVENT_VERSION:
        return HotmartDecision(
            False, "unsupported_version", "ignore", event_id, event_type
        )

    return HotmartDecision(
        True, "accepted", "persist", event_id, event_type
    )
