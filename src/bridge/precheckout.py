"""Adapter for the provisional Joana pre-checkout form contract.

The external shape in this module is deliberately emulated and replaceable.  It
normalizes into a provider-independent event and can never authorize activation
by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

PRECHECKOUT_EVENT_TYPE = "PRECHECKOUT_FORM_SUBMITTED"
EMULATED_CONTRACT_VERSION = "1.0.0-emulated"
_E164 = re.compile(r"\+[1-9][0-9]{7,14}")
_EMULATED_EVENT_KEYS = {"id", "event", "version", "created_at", "lead"}
_EMULATED_LEAD_KEYS = {"full_name", "phone_e164"}


@dataclass(frozen=True)
class PrecheckoutScope:
    """Server-owned commercial scope for the emulated form adapter."""

    tenant_ref: str
    funnel_ref: str
    landing_ref: str
    product_ref: str
    offer_ref: str
    consent_copy_version: str


@dataclass(frozen=True)
class PrecheckoutSubmission:
    external_submission_id: str
    event_type: str
    contract_version: str
    submitted_at: datetime
    tenant_ref: str
    funnel_ref: str
    landing_ref: str
    full_name: str
    normalized_email: str | None
    normalized_phone: str
    phone_country_iso: str | None
    product_ref: str
    offer_ref: str
    terms_accepted: bool
    privacy_accepted: bool
    whatsapp_contact_authorized: bool
    consent_copy_version: str
    provisional: bool = True
    provider_observed: bool = False
    activation_authorized: bool = False

    def as_canonical_payload(self) -> dict[str, object]:
        """Return the stable internal representation persisted by the bridge."""
        return {
            "event_type": self.event_type,
            "contract_version": self.contract_version,
            "external_submission_id": self.external_submission_id,
            "submitted_at": self.submitted_at.isoformat().replace("+00:00", "Z"),
            "source": {
                "tenant_ref": self.tenant_ref,
                "funnel_ref": self.funnel_ref,
                "landing_ref": self.landing_ref,
            },
            "identity": {"phone": self.normalized_phone},
            "lead": {
                "full_name": self.full_name,
                "phone_country_iso": self.phone_country_iso,
            },
            "commerce": {
                "product_ref": self.product_ref,
                "offer_ref": self.offer_ref,
            },
            "consent": {
                "terms_accepted": self.terms_accepted,
                "privacy_accepted": self.privacy_accepted,
                "whatsapp_contact": self.whatsapp_contact_authorized,
                "copy_version": self.consent_copy_version,
            },
            "assurance": {
                "provisional": self.provisional,
                "provider_observed": self.provider_observed,
                "activation_authorized": self.activation_authorized,
            },
        }


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _required_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _timestamp(value: object) -> datetime | None:
    raw = _required_text(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def parse_emulated_precheckout_submission(
    payload: object,
    *,
    scope: PrecheckoutScope,
) -> PrecheckoutSubmission | None:
    """Parse the provisional V1 shape into a non-authoritative internal event."""
    event = _object(payload)
    lead = _object(event.get("lead"))

    submission_id = _required_text(event.get("id"))
    submitted_at = _timestamp(event.get("created_at"))
    full_name = _required_text(lead.get("full_name"))
    phone_e164 = _required_text(lead.get("phone_e164"))
    scope_values = (
        scope.tenant_ref,
        scope.funnel_ref,
        scope.landing_ref,
        scope.product_ref,
        scope.offer_ref,
        scope.consent_copy_version,
    )

    if (
        set(event) != _EMULATED_EVENT_KEYS
        or set(lead) != _EMULATED_LEAD_KEYS
        or event.get("event") != PRECHECKOUT_EVENT_TYPE
        or event.get("version") != EMULATED_CONTRACT_VERSION
        or submission_id is None
        or submitted_at is None
        or full_name is None
        or phone_e164 is None
        or _E164.fullmatch(phone_e164) is None
        or any(_required_text(value) is None for value in scope_values)
    ):
        return None

    return PrecheckoutSubmission(
        external_submission_id=submission_id,
        event_type=PRECHECKOUT_EVENT_TYPE,
        contract_version=EMULATED_CONTRACT_VERSION,
        submitted_at=submitted_at,
        tenant_ref=scope.tenant_ref.strip(),
        funnel_ref=scope.funnel_ref.strip(),
        landing_ref=scope.landing_ref.strip(),
        full_name=full_name,
        normalized_email=None,
        normalized_phone=phone_e164[1:],
        phone_country_iso=None,
        product_ref=scope.product_ref.strip(),
        offer_ref=scope.offer_ref.strip(),
        terms_accepted=False,
        privacy_accepted=False,
        whatsapp_contact_authorized=False,
        consent_copy_version=scope.consent_copy_version.strip(),
    )