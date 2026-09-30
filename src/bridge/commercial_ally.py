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
_OFFER_CODE = re.compile(r"[A-Za-z0-9]{4,32}")
_OFFER_LANDING_KEYS = frozenset(
    {"offer_code", "site", "landing_id", "page_host", "page_path"}
)


def _canonical_page_path(path: str) -> bool:
    return path.startswith("/") and "?" not in path and "#" not in path


@dataclass(frozen=True)
class OfferLanding:
    """La landing donde se ofrece una oferta del binding.

    El formulario del precheckout de esa landing se admite con esa oferta, en
    ese sitio, host y ruta. La oferta por defecto usa los campos ``lead_*``.
    """

    offer_code: str
    site: str
    landing_id: str
    page_host: str
    page_path: str

    @classmethod
    def from_json(cls, value: object) -> OfferLanding:
        """Una landing del manifiesto JSON o de la fila durable del binding."""

        if (
            not isinstance(value, dict)
            or set(value) != _OFFER_LANDING_KEYS
            or not all(isinstance(item, str) for item in value.values())
        ):
            raise ValueError(
                "additional_offer_landings items must be objects with exactly "
                "offer_code, site, landing_id, page_host and page_path"
            )
        return cls(**value)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.offer_code, str)
            or not self.offer_code
            or any(char.isspace() for char in self.offer_code)
        ):
            raise ValueError("additional_offer_landings offer_code must not be blank")
        if any(
            not isinstance(value, str) or _REF.fullmatch(value) is None
            for value in (self.site, self.landing_id)
        ):
            raise ValueError(
                "additional_offer_landings site and landing_id must be canonical slugs"
            )
        if not isinstance(self.page_host, str) or _HOST.fullmatch(self.page_host) is None:
            raise ValueError(
                "additional_offer_landings page_host must be a canonical hostname"
            )
        if not isinstance(self.page_path, str) or not _canonical_page_path(
            self.page_path
        ):
            raise ValueError(
                "additional_offer_landings page_path must be one canonical absolute path"
            )


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
    # Otras ofertas del mismo producto, una por landing. `offer_code` sigue
    # siendo la oferta por defecto. Opcional en el manifiesto JSON.
    additional_offer_codes: tuple[str, ...] = ()
    # La landing de cada oferta adicional, en el mismo orden que
    # ``additional_offer_codes``. Vacio: el formulario del precheckout entra
    # solo por la landing de la oferta por defecto. Opcional en el manifiesto
    # JSON.
    additional_offer_landings: tuple[OfferLanding, ...] = ()

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
        optional = {"additional_offer_codes", "additional_offer_landings"}
        if not expected - optional <= set(payload) <= expected:
            raise ValueError("commercial ally manifest must contain exactly the supported keys")
        additional = payload.get("additional_offer_codes", [])
        if not isinstance(additional, list) or not all(
            isinstance(code, str) for code in additional
        ):
            raise ValueError("additional_offer_codes must be a JSON list of strings")
        payload["additional_offer_codes"] = tuple(additional)
        landings = payload.get("additional_offer_landings", [])
        if not isinstance(landings, list):
            raise ValueError("additional_offer_landings must be a JSON list of objects")
        payload["additional_offer_landings"] = tuple(
            OfferLanding.from_json(landing) for landing in landings
        )
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
        if not _canonical_page_path(self.lead_page_path):
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
        if not isinstance(self.additional_offer_codes, tuple) or any(
            not isinstance(code, str) or _OFFER_CODE.fullmatch(code) is None
            for code in self.additional_offer_codes
        ):
            raise ValueError("additional_offer_codes must be alphanumeric offer codes")
        if (
            len(self.additional_offer_codes) > 16
            or len(set(self.accepted_offer_codes)) != len(self.accepted_offer_codes)
        ):
            raise ValueError(
                "additional_offer_codes must be at most 16 distinct codes "
                "different from offer_code"
            )
        if not isinstance(self.additional_offer_landings, tuple) or any(
            not isinstance(landing, OfferLanding)
            for landing in self.additional_offer_landings
        ):
            raise ValueError("additional_offer_landings must be a tuple of OfferLanding")
        if self.additional_offer_landings and tuple(
            landing.offer_code for landing in self.additional_offer_landings
        ) != self.additional_offer_codes:
            raise ValueError(
                "additional_offer_landings must declare one landing per additional "
                "offer, in the order of additional_offer_codes"
            )
        pairs = [(landing.site, landing.landing_id) for landing in self.offer_landings]
        if len(set(pairs)) != len(pairs):
            raise ValueError(
                "additional_offer_landings must not repeat a site and landing_id"
            )

    @property
    def accepted_offer_codes(self) -> tuple[str, ...]:
        """La oferta por defecto primero, despues las demas del binding."""

        return (self.offer_code, *self.additional_offer_codes)

    @property
    def offer_landings(self) -> tuple[OfferLanding, ...]:
        """Las landings donde se admite el formulario: la por defecto primero."""

        default = OfferLanding(
            offer_code=self.offer_code,
            site=self.lead_site,
            landing_id=self.lead_landing_id,
            page_host=self.lead_page_host,
            page_path=self.lead_page_path,
        )
        return (default, *self.additional_offer_landings)

    def offer_landing(self, site: str, landing_id: str) -> OfferLanding | None:
        """La oferta declarada para esa landing, o ``None`` si no es del binding."""

        for landing in self.offer_landings:
            if landing.site == site and landing.landing_id == landing_id:
                return landing
        return None

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
