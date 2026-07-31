"""Identity resolution orchestrator for Hotmart cart-abandonment events.

Takes a webhook_event in 'received' status, resolves the buyer's identity
against Supabase, creates or updates contacts / contact_points / recovery_cases,
and builds a SituationReport for the agent — all deterministically, without
invoking the LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bridge.hotmart import HotmartBuyerData, parse_hotmart_payload
from bridge.supabase import (
    ContactMatch,
    SituationReport,
    SupabaseClient,
    SupabaseError,
)

# Grace period before the first contact attempt (hours).
DEFAULT_GRACE_HOURS = 1


class ResolutionError(RuntimeError):
    """Raised when identity resolution cannot complete."""


async def resolve_event(
    *,
    webhook_event_id: str,
    payload: dict[str, object],
    supabase: SupabaseClient,
    grace_hours: int = DEFAULT_GRACE_HOURS,
) -> SituationReport:
    """Resolve identity for one webhook event and return a situation report.

    Steps:
    1. Parse buyer data from the Hotmart payload.
    2. Look up existing contact by email, then by phone.
    3. If not found, create contact + contact_points.
    4. If found, ensure contact_points exist for new data.
    5. Create a recovery_case linked to the contact and event.
    6. Fetch conversations, recovery_cases, channel_identities.
    7. Build and return the SituationReport.
    8. Mark the webhook_event as 'processed'.
    """
    buyer = parse_hotmart_payload(payload)
    if buyer is None:
        await supabase.update_event_status(
            event_id=webhook_event_id,
            status="failed",
            error="invalid_payload_structure",
        )
        raise ResolutionError("invalid_payload_structure")

    # ── Step 2: Look up existing contact ────────────────────────────
    match: ContactMatch | None = None
    strategy: str | None = None

    if buyer.buyer_email is not None:
        try:
            match = await supabase.find_contact_by_email(buyer.buyer_email)
        except SupabaseError:
            match = None
        if match is not None:
            strategy = "existing_identity_by_email"

    if match is None and buyer.buyer_phone is not None:
        try:
            match = await supabase.find_contact_by_phone(buyer.buyer_phone)
        except SupabaseError:
            match = None
        if match is not None:
            strategy = "existing_identity_by_phone"

    # ── Step 3: Create contact if not found ────────────────────────
    contact_id: str
    resolution_status: str

    if match is not None:
        contact_id = match.contact_id
        resolution_status = "resolved"
    else:
        try:
            contact_id = await supabase.create_contact(
                full_name=buyer.buyer_name,
                email=buyer.buyer_email,
                phone=buyer.buyer_phone,
                country_iso=buyer.checkout_country_iso,
            )
        except SupabaseError as exc:
            await supabase.update_event_status(
                event_id=webhook_event_id,
                status="failed",
                error="create_contact_failed",
            )
            raise ResolutionError("create_contact_failed") from exc
        resolution_status = "resolved"
        strategy = "new_contact_from_hotmart"

    # ── Step 4: Ensure contact_points exist ────────────────────────
    if buyer.buyer_email is not None:
        try:
            await supabase.create_contact_point(
                contact_id=contact_id,
                point_type="email",
                raw_value=buyer.buyer_email,
                normalized_value=buyer.buyer_email,
                source="hotmart",
                source_event_id=webhook_event_id,
            )
        except SupabaseError:
            pass  # Best-effort; duplicates are silently ignored

    if buyer.buyer_phone is not None:
        try:
            await supabase.create_contact_point(
                contact_id=contact_id,
                point_type="phone",
                raw_value=buyer.buyer_phone,
                normalized_value=buyer.buyer_phone,
                source="hotmart",
                source_event_id=webhook_event_id,
            )
        except SupabaseError:
            pass

    # ── Step 5: Create recovery_case ───────────────────────────────
    grace_expires = datetime.now(timezone.utc) + timedelta(hours=grace_hours)
    try:
        recovery_case_id = await supabase.create_recovery_case(
            contact_id=contact_id,
            abandonment_event_id=webhook_event_id,
            external_product_id=(
                str(buyer.product_id) if buyer.product_id is not None else None
            ),
            product_name=buyer.product_name,
            offer_code=buyer.offer_code,
            grace_expires_at=grace_expires.isoformat(),
        )
    except SupabaseError as exc:
        await supabase.update_event_status(
            event_id=webhook_event_id,
            status="failed",
            error="create_recovery_case_failed",
        )
        raise ResolutionError("create_recovery_case_failed") from exc

    # ── Log the resolution attempt ──────────────────────────────────
    try:
        await supabase.log_resolution_attempt(
            recovery_case_id=recovery_case_id,
            channel="whatsapp",
            strategy=strategy or "other",
            status="matched" if match is not None else "not_found",
            confidence=1.0 if match is not None else None,
        )
    except SupabaseError:
        pass  # Best-effort logging

    # ── Step 6: Fetch conversations, recovery_cases, identities ────
    conversations = []
    recovery_cases = []
    channel_identities = []

    try:
        conversations = await supabase.fetch_conversations(contact_id=contact_id)
    except SupabaseError:
        pass
    try:
        recovery_cases = await supabase.fetch_recovery_cases(contact_id=contact_id)
    except SupabaseError:
        pass
    try:
        channel_identities = await supabase.fetch_channel_identities(
            contact_id=contact_id
        )
    except SupabaseError:
        pass

    # ── Step 7: Build SituationReport ──────────────────────────────
    has_active = any(
        c.status in ("active", "awaiting_contact", "awaiting_agent")
        and not c.human_takeover
        for c in conversations
    )
    has_open = any(
        rc.status in ("grace_period", "active", "paused")
        for rc in recovery_cases
    )
    contact_blocked = (
        match is not None
        and match.contact_permission in ("opted_out", "blocked", "restricted", "do_not_contact")
    ) or any(
        ci.identity_status in ("blocked", "unreachable")
        for ci in channel_identities
        if ci.channel == "whatsapp"
    )

    report = SituationReport(
        event_id=buyer.event_id,
        event_type=buyer.event_type,
        source="hotmart",
        buyer_name=buyer.buyer_name,
        buyer_email=buyer.buyer_email,
        buyer_phone=buyer.buyer_phone,
        product_name=buyer.product_name,
        offer_code=buyer.offer_code,
        checkout_country_iso=buyer.checkout_country_iso,
        contact_id=contact_id,
        contact_match=match,
        identity_resolution_status=resolution_status,
        identity_resolution_strategy=strategy,
        conversations=conversations,
        recovery_cases=recovery_cases,
        channel_identities=channel_identities,
        has_active_conversation=has_active,
        has_open_recovery_case=has_open,
        phone_available=buyer.buyer_phone is not None,
        contact_blocked=contact_blocked,
    )

    # ── Step 8: Mark event as processed ─────────────────────────────
    await supabase.update_event_status(
        event_id=webhook_event_id,
        status="processed",
    )

    return report
