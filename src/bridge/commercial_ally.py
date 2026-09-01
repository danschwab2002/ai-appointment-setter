"""Versioned non-secret binding for one isolated commercial ally runtime."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

_REF = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_CURRENCY = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class CommercialAllyConfig:
    """Customer-owned identifiers required by the first portable adapters.

    Values are non-secret and describe one single-tenant deployment. External
    credentials remain in the deployment secret store.
    """

    tenant_ref: str
    funnel_ref: str
    binding_version: int
    ally_ref: str
    lead_ally_name: str
    lead_site: str
    lead_landing_id: str
    lead_page_host: str
    lead_page_path: str
    product_hotlink: str
    product_name: str
    product_price: Decimal
    currency: str
    offer_code: str
    consent_copy_version: str
    hotmart_product_id: int
    chatwoot_account_id: int
    chatwoot_inbox_id: int
    inbound_scope_key: str
    inbound_scope_version: int

    @classmethod
    def from_json_file(cls, path: Path) -> CommercialAllyConfig:
        """Load one exact, non-secret customer binding from a JSON manifest."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("commercial ally manifest must be readable JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("commercial ally manifest must be a JSON object")
        expected = {field.name for field in fields(cls)}
        if set(payload) != expected:
            raise ValueError("commercial ally manifest must contain exactly the supported keys")
        price = payload.get("product_price")
        if isinstance(price, bool) or not isinstance(price, (str, int, float)):
            raise ValueError("product_price must be a JSON string or number")
        try:
            payload["product_price"] = Decimal(str(price))
            return cls(**payload)
        except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("commercial ally manifest contains invalid values") from exc

    def __post_init__(self) -> None:
        refs = (
            self.tenant_ref,
            self.funnel_ref,
            self.ally_ref,
            self.lead_site,
            self.lead_landing_id,
        )
        if any(_REF.fullmatch(value) is None for value in refs):
            raise ValueError("commercial ally references must be canonical slugs")
        if not self.lead_ally_name.strip():
            raise ValueError("lead_ally_name must not be blank")
        if _HOST.fullmatch(self.lead_page_host) is None:
            raise ValueError("lead_page_host must be a canonical hostname")
        if (
            not self.lead_page_path.startswith("/")
            or "?" in self.lead_page_path
            or "#" in self.lead_page_path
        ):
            raise ValueError("lead_page_path must be one canonical absolute path")
        if not self.product_hotlink or "/" in self.product_hotlink:
            raise ValueError("product_hotlink must be one non-empty path segment")
        if not self.product_name.strip():
            raise ValueError("product_name must not be blank")
        if not self.product_price.is_finite() or self.product_price <= 0:
            raise ValueError("product_price must be finite and positive")
        if _CURRENCY.fullmatch(self.currency) is None:
            raise ValueError("currency must be an uppercase ISO-style code")
        if not self.offer_code or any(char.isspace() for char in self.offer_code):
            raise ValueError("offer_code must not be blank or contain whitespace")
        if not self.consent_copy_version.strip():
            raise ValueError("consent_copy_version must not be blank")
        if type(self.hotmart_product_id) is not int or self.hotmart_product_id < 1:
            raise ValueError("hotmart_product_id must be a positive integer")
        for field_name, value in (
            ("binding_version", self.binding_version),
            ("chatwoot_account_id", self.chatwoot_account_id),
            ("chatwoot_inbox_id", self.chatwoot_inbox_id),
            ("inbound_scope_version", self.inbound_scope_version),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if _REF.fullmatch(self.inbound_scope_key) is None:
            raise ValueError("inbound_scope_key must be a canonical slug")

    @property
    def lead_page_url(self) -> str:
        return f"https://{self.lead_page_host}{self.lead_page_path}"

    @property
    def checkout_url(self) -> str:
        return (
            f"https://pay.hotmart.com/{self.product_hotlink}"
            f"?off={self.offer_code}"
        )


JOHANNA_COMMERCIAL_ALLY = CommercialAllyConfig(
    tenant_ref="lancemos",
    funnel_ref="psicologajohanna",
    binding_version=1,
    ally_ref="johanna",
    lead_ally_name="Psicologa Johanna",
    lead_site="psicologajohanna",
    lead_landing_id="ads-a",
    lead_page_host="psicologajohanna.com",
    lead_page_path="/ldla/evg/vsl/ads-a",
    product_hotlink="F106691755G",
    product_name="Liberate De La Ansiedad",
    product_price=Decimal("49"),
    currency="USD",
    offer_code="bxjge6zq",
    consent_copy_version="johanna-precheckout-whatsapp-disclosure-v1",
    hotmart_product_id=8104005,
    chatwoot_account_id=1,
    chatwoot_inbox_id=9,
    inbound_scope_key="libre-de-ansiedad-inbound",
    inbound_scope_version=2,
)
