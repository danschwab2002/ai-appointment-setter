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
EVENT_PURCHASE_APPROVED = "PURCHASE_APPROVED"
EVENT_VERSION = "2.0.0"
SUPPORTED_EVENT_TYPES = frozenset({
    EVENT_CART_ABANDONMENT,
    EVENT_PURCHASE_APPROVED,
})


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


@dataclass(frozen=True)
class HotmartPurchaseData:
    """Identifiers required to correlate one approved Hotmart purchase."""

    event_id: str
    creation_date_ms: int
    approved_date_ms: int
    transaction: str
    buyer_email: str | None
    buyer_phone: str | None
    product_id: int
    product_ucode: str | None
    offer_code: str | None


# ── Normalisation helpers ────────────────────────────────────────────

# Hotmart normally sends DDI and digits without "+". Accept conventional
# display separators, but reject letters, JID suffixes, and other content
# before normalization.
_NON_DIGIT = re.compile(r"\D")
_PHONE_INPUT = re.compile(r"\+?[0-9 ()-]+")
_TRANSACTION_REFERENCE = re.compile(r"HP[A-Z0-9]{6,62}")
_MAX_DATETIME_TIMESTAMP_MS = 253_402_300_799_999


def normalize_email(raw: str | None) -> str | None:
    """Lower-case and trim an email, returning ``None`` if empty."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def normalize_phone(raw: str | None) -> str | None:
    """Validate phone syntax and return bare digits, or ``None``."""
    if not isinstance(raw, str) or _PHONE_INPUT.fullmatch(raw) is None:
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


def parse_hotmart_purchase_payload(payload: object) -> HotmartPurchaseData | None:
    """Extract the fail-closed correlation fields from PURCHASE_APPROVED v2."""
    event = _json_object(payload)
    data = _json_object(event.get("data"))
    buyer = _json_object(data.get("buyer"))
    product = _json_object(data.get("product"))
    purchase = _json_object(data.get("purchase"))
    offer = _json_object(purchase.get("offer"))

    event_id = _str(event.get("id"))
    creation_date = _int(event.get("creation_date"))
    approved_date = _int(purchase.get("approved_date"))
    transaction = _str(purchase.get("transaction"))
    product_id = _int(product.get("id"))
    buyer_email = normalize_email(buyer.get("email"))
    buyer_phone = normalize_phone(buyer.get("checkout_phone"))
    if (
        event_id is None
        or event.get("event") != EVENT_PURCHASE_APPROVED
        or event.get("version") != EVENT_VERSION
        or purchase.get("status") != "APPROVED"
        or creation_date is None
        or not 0 <= creation_date <= _MAX_DATETIME_TIMESTAMP_MS
        or approved_date is None
        or not 0 <= approved_date <= _MAX_DATETIME_TIMESTAMP_MS
        or transaction is None
        or _TRANSACTION_REFERENCE.fullmatch(transaction) is None
        or product_id is None
        or (buyer_email is None and buyer_phone is None)
    ):
        return None

    return HotmartPurchaseData(
        event_id=event_id,
        creation_date_ms=creation_date,
        approved_date_ms=approved_date,
        transaction=transaction,
        buyer_email=buyer_email,
        buyer_phone=buyer_phone,
        product_id=product_id,
        product_ucode=_str(product.get("ucode")),
        offer_code=_str(offer.get("code")),
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
    if not isinstance(event_type, str) or event_type not in SUPPORTED_EVENT_TYPES:
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
