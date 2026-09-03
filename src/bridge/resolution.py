"""Identity resolution orchestrator for Hotmart cart-abandonment events.

Takes a webhook_event in 'received' status, resolves the buyer's identity
against Supabase, creates or updates contacts / contact_points / recovery_cases,
and builds a SituationReport for the agent — all deterministically, without
invoking the LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bridge.commercial_ally import CommercialAllyConfig
from bridge.hotmart import (
    EVENT_PURCHASE_CANCELED,
    HotmartBuyerData,
    parse_hotmart_payload,
    parse_hotmart_payment_failure_buyer_payload,
)
from bridge.messaging import is_allowed_whatsapp_target
from bridge.supabase import (
    ContactMatch,
    PilotBoundaryConfig,
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
    policy_key: str | None = None,
    policy_version: int | None = None,
    allowed_jid: str | None = None,
    chatwoot_account_id: int | None = None,
    chatwoot_inbox_id: int | None = None,
    pilot_boundary: PilotBoundaryConfig | None = None,
    commercial_ally_config: CommercialAllyConfig | None = None,
    event_type: str | None = None,
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
    if event_type == EVENT_PURCHASE_CANCELED:
        buyer = (
            parse_hotmart_payment_failure_buyer_payload(
                payload,
                config=commercial_ally_config,
            )
            if commercial_ally_config is not None
            else None
        )
    else:
        buyer = (
            parse_hotmart_payload(payload, config=commercial_ally_config)
            if commercial_ally_config is not None
            else parse_hotmart_payload(payload)
        )
    if buyer is None:
        await supabase.update_event_status(
            event_id=webhook_event_id,
            status="failed",
            error="invalid_payload_structure",
        )
        raise ResolutionError("invalid_payload_structure")

    # ── Step 2: Look up existing contact ────────────────────────────
    email_match: ContactMatch | None = None
    phone_match: ContactMatch | None = None
    authoritative_context_complete = True

    if buyer.buyer_email is not None:
        try:
            email_match = await supabase.find_contact_by_email(buyer.buyer_email)
        except SupabaseError as exc:
            if str(exc).endswith("_ambiguous"):
                await supabase.update_event_status(
                    event_id=webhook_event_id,
                    status="failed",
                    error="identity_ambiguous",
                )
                raise ResolutionError("identity_ambiguous") from exc
            authoritative_context_complete = False

    if buyer.buyer_phone is not None:
        try:
            phone_match = await supabase.find_contact_by_phone(buyer.buyer_phone)
        except SupabaseError as exc:
            if str(exc).endswith("_ambiguous"):
                await supabase.update_event_status(
                    event_id=webhook_event_id,
                    status="failed",
                    error="identity_ambiguous",
                )
                raise ResolutionError("identity_ambiguous") from exc
            authoritative_context_complete = False

    if (
        email_match is not None
        and phone_match is not None
        and email_match.contact_id != phone_match.contact_id
    ):
        await supabase.update_event_status(
            event_id=webhook_event_id,
            status="failed",
            error="identity_ambiguous",
        )
        raise ResolutionError("identity_ambiguous")

    match = email_match or phone_match
    strategy: str | None = None
    if email_match is not None:
        strategy = "existing_identity_by_email"
    elif phone_match is not None:
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
        except SupabaseError as exc:
            await supabase.update_event_status(
                event_id=webhook_event_id,
                status="failed",
                error="identity_binding_failed",
            )
            raise ResolutionError("identity_binding_failed") from exc

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
        except SupabaseError as exc:
            await supabase.update_event_status(
                event_id=webhook_event_id,
                status="failed",
                error="identity_binding_failed",
            )
            raise ResolutionError("identity_binding_failed") from exc

    # ── Step 5: Create the recovery case, or the complete durable plan ──
    durable_plan_enabled = policy_key is not None and policy_version is not None
    try:
        if policy_key is not None and policy_version is not None:
            if buyer.product_id is None or buyer.product_name is None:
                raise SupabaseError("plan_cart_recovery_missing_product")
            abandoned_at = datetime.fromtimestamp(
                buyer.creation_date_ms / 1000,
                tz=timezone.utc,
            ).isoformat()
            portable_identity_allowed = (
                commercial_ally_config is not None
                and pilot_boundary is not None
                and buyer.buyer_phone is not None
                and chatwoot_account_id
                == commercial_ally_config.chatwoot_account_id
                and chatwoot_inbox_id == commercial_ally_config.chatwoot_inbox_id
                and pilot_boundary.tenant_key
                == commercial_ally_config.tenant_ref
                and pilot_boundary.channel_provider == "waba"
                and pilot_boundary.channel_account_ref
                == f"chatwoot-inbox:{commercial_ally_config.chatwoot_inbox_id}"
            )
            legacy_identity_allowed = (
                buyer.buyer_phone is not None
                and chatwoot_account_id is not None
                and chatwoot_inbox_id is not None
                and is_allowed_whatsapp_target(buyer.buyer_phone, allowed_jid)
            )
            identity_allowed = portable_identity_allowed or legacy_identity_allowed
            planner = (
                supabase.plan_payment_failure_recovery
                if buyer.event_type == EVENT_PURCHASE_CANCELED
                else supabase.plan_cart_recovery
            )
            plan = await planner(
                webhook_event_id=webhook_event_id,
                contact_id=contact_id,
                external_product_id=str(buyer.product_id),
                product_name=buyer.product_name,
                offer_code=buyer.offer_code,
                policy_key=policy_key,
                policy_version=policy_version,
                abandoned_at=abandoned_at,
                chatwoot_account_id=(
                    chatwoot_account_id if identity_allowed else None
                ),
                chatwoot_inbox_id=(chatwoot_inbox_id if identity_allowed else None),
                external_user_id=(buyer.buyer_phone if identity_allowed else None),
                pilot_boundary=pilot_boundary,
            )
            recovery_case_id = plan.recovery_case_id
        else:
            grace_expires = datetime.now(timezone.utc) + timedelta(
                hours=grace_hours
            )
            recovery_case_id = await supabase.create_recovery_case(
                contact_id=contact_id,
                abandonment_event_id=webhook_event_id,
                external_product_id=(
                    str(buyer.product_id)
                    if buyer.product_id is not None
                    else None
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

    # Durable planning records a matched selected identity in the same SQL
    # transaction. The legacy path still owns its best-effort audit write.
    if not durable_plan_enabled:
        audit_strategy = (
            strategy
            if strategy in {
                "existing_identity_by_email",
                "existing_identity_by_phone",
            }
            else "other"
        )
        try:
            await supabase.log_resolution_attempt(
                recovery_case_id=recovery_case_id,
                channel="whatsapp",
                strategy=audit_strategy,
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
        authoritative_context_complete = False
    try:
        recovery_cases = await supabase.fetch_recovery_cases(contact_id=contact_id)
    except SupabaseError:
        authoritative_context_complete = False
    try:
        channel_identities = await supabase.fetch_channel_identities(
            contact_id=contact_id
        )
    except SupabaseError:
        authoritative_context_complete = False

    # ── Step 7: Build SituationReport ──────────────────────────────
    has_active = any(
        c.status in ("active", "awaiting_contact", "awaiting_agent")
        and not c.human_takeover
        for c in conversations
    )
    any_human_takeover = any(c.human_takeover for c in conversations)
    has_open = any(
        rc.recovery_case_id != recovery_case_id
        and rc.status in ("grace_period", "active", "paused")
        for rc in recovery_cases
    )
    contact_blocked = (
        match is not None
        and (
            match.contact_permission in ("opted_out", "blocked", "restricted")
            or match.lifecycle_status == "do_not_contact"
        )
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
        authoritative_context_complete=authoritative_context_complete,
        any_conversation_human_takeover=any_human_takeover,
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
