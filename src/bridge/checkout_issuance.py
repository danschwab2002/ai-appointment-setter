"""Build a minimal, server-owned Hotmart checkout issuance."""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit


_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class CheckoutIssuanceUnavailable(ValueError):
    """A checkout issuance cannot be built from the supplied authority."""


@dataclass(frozen=True, slots=True)
class CheckoutOffer:
    tenant_ref: str
    product_ref: str
    landing_ref: str
    offer_code: str
    checkout_base_url: str
    checkout_mode: int

    def __post_init__(self) -> None:
        for field_name in ("tenant_ref", "product_ref", "landing_ref", "offer_code"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
                raise ValueError("invalid_offer")
        if (
            isinstance(self.checkout_mode, bool)
            or not isinstance(self.checkout_mode, int)
            or self.checkout_mode < 1
        ):
            raise ValueError("invalid_offer")
        try:
            parsed = urlsplit(self.checkout_base_url)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_checkout_base_url") from exc
        if (
            parsed.scheme != "https"
            or parsed.netloc != "pay.hotmart.com"
            or parsed.path != f"/{self.product_ref}"
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("invalid_checkout_base_url")


@dataclass(frozen=True, slots=True)
class CheckoutIssuance:
    final_url: str
    source_value: str
    sck_value: str
    issuance_ulid: str


def generate_issuance_ulid(*, timestamp_ms: int | None = None) -> str:
    """Generate a Crockford ULID without adding a runtime dependency."""

    current_ms = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if isinstance(current_ms, bool) or not isinstance(current_ms, int) or not (
        0 <= current_ms < 2**48
    ):
        raise ValueError("invalid_ulid_timestamp")
    value = (current_ms << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(encoded)


def build_checkout_issuance(
    *, offer: CheckoutOffer, issuance_ulid: str
) -> CheckoutIssuance:
    """Build the exact V1 Hermes attribution URL from a catalog offer."""

    if not isinstance(issuance_ulid, str) or _ULID.fullmatch(issuance_ulid) is None:
        raise CheckoutIssuanceUnavailable("invalid_issuance_ulid")
    source_value = "hermes"
    sck_value = f"hermes|v1|{issuance_ulid}"
    query = urlencode(
        (
            ("off", offer.offer_code),
            ("checkoutMode", str(offer.checkout_mode)),
            ("src", source_value),
            ("sck", sck_value),
        )
    )
    return CheckoutIssuance(
        final_url=f"{offer.checkout_base_url}?{query}",
        source_value=source_value,
        sck_value=sck_value,
        issuance_ulid=issuance_ulid,
    )
