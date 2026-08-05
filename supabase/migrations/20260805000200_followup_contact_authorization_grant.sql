BEGIN;

-- Cart abandonment is the authorization: when the durable plan resolves a
-- WhatsApp identity for a cart-recovery case, grant an 'allowed'
-- contact_authorizations row in the SAME transaction, derived from the
-- Hotmart cart-abandonment evidence. The grant is idempotent (no duplicate
-- on replay) and never overrides an active denial/restriction/unknown: it is
-- written only when no active authorization row already exists for the tuple.
-- Opt-out remains authoritative because the reevaluation RPC checks
-- denied/restricted BEFORE it checks allowed.

CREATE OR REPLACE FUNCTION public.plan_cart_recovery_with_identity(
    p_webhook_event_id uuid,
    p_contact_id uuid,
    p_external_product_id text,
    p_product_name text,
    p_offer_code text,
    p_policy_key text,
    p_policy_version integer,
    p_abandoned_at timestamptz,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_external_user_id text
)
RETURNS TABLE (
    recovery_case_id uuid,
    followup_sequence_id uuid,
    scheduled_action_id uuid,
    created boolean
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
DECLARE
    v_plan record;
    v_contact public.contacts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_identity public.channel_identities%rowtype;
    v_account_id text;
    v_now timestamptz := clock_timestamp();
BEGIN
    IF p_chatwoot_account_id IS NULL OR p_chatwoot_account_id < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'chatwoot_account_id_invalid';
    END IF;
    IF p_chatwoot_inbox_id IS NULL OR p_chatwoot_inbox_id < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'chatwoot_inbox_id_invalid';
    END IF;
    IF p_external_user_id IS NULL
       OR btrim(p_external_user_id) = ''
       OR NOT (p_external_user_id ~ '^[0-9]+$') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'external_user_id_invalid';
    END IF;

    v_account_id := 'chatwoot:' || p_chatwoot_account_id::text;

    SELECT * INTO STRICT v_plan
    FROM public.plan_cart_recovery(
        p_webhook_event_id,
        p_contact_id,
        p_external_product_id,
        p_product_name,
        p_offer_code,
        p_policy_key,
        p_policy_version,
        p_abandoned_at
    );

    -- Preserve the global lock order already established by plan_cart_recovery.
    SELECT * INTO STRICT v_contact
    FROM public.contacts c
    WHERE c.id = p_contact_id
    FOR UPDATE;

    SELECT * INTO STRICT v_case
    FROM public.recovery_cases rc
    WHERE rc.id = v_plan.recovery_case_id
      AND rc.contact_id = p_contact_id
    FOR UPDATE;

    SELECT * INTO v_identity
    FROM public.channel_identities ci
    WHERE ci.channel = 'whatsapp'
      AND ci.account_id = v_account_id
      AND ci.external_user_id = p_external_user_id
    FOR UPDATE;

    IF v_identity.id IS NULL THEN
        BEGIN
            INSERT INTO public.channel_identities (
                contact_id,
                channel,
                account_id,
                external_user_id,
                identity_status,
                metadata
            ) VALUES (
                p_contact_id,
                'whatsapp',
                v_account_id,
                p_external_user_id,
                'active',
                jsonb_build_object('inbox_id', p_chatwoot_inbox_id)
            )
            RETURNING * INTO v_identity;
        EXCEPTION WHEN unique_violation THEN
            SELECT * INTO STRICT v_identity
            FROM public.channel_identities ci
            WHERE ci.channel = 'whatsapp'
              AND ci.account_id = v_account_id
              AND ci.external_user_id = p_external_user_id
            FOR UPDATE;
        END;
    END IF;

    IF v_identity.contact_id <> p_contact_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'channel_identity_contact_mismatch';
    END IF;
    IF v_identity.identity_status <> 'active' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'channel_identity_not_active';
    END IF;
    IF v_identity.metadata ? 'inbox_id'
       AND v_identity.metadata ->> 'inbox_id' <> p_chatwoot_inbox_id::text THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'channel_identity_inbox_mismatch';
    END IF;
    IF v_case.selected_channel_identity_id IS NOT NULL
       AND v_case.selected_channel_identity_id <> v_identity.id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'recovery_case_channel_identity_mismatch';
    END IF;

    UPDATE public.channel_identities
    SET metadata = metadata || jsonb_build_object('inbox_id', p_chatwoot_inbox_id),
        updated_at = clock_timestamp()
    WHERE id = v_identity.id;

    UPDATE public.recovery_cases
    SET selected_channel_identity_id = v_identity.id,
        identity_resolution_status = 'resolved',
        identity_resolution_error = NULL,
        identity_resolution_last_attempt_at = CASE
            WHEN identity_resolution_status = 'resolved'
                THEN identity_resolution_last_attempt_at
            ELSE clock_timestamp()
        END,
        identity_resolution_attempt_count = CASE
            WHEN identity_resolution_status = 'resolved'
                THEN identity_resolution_attempt_count
            ELSE identity_resolution_attempt_count + 1
        END
    WHERE id = v_case.id;

    -- Cart abandonment IS the authorization. Grant 'allowed' for
    -- whatsapp/cart_recovery only when no active authorization row already
    -- exists for this contact+channel+purpose. This is idempotent under replay
    -- and never overrides an active denied/restricted/unknown row (opt-out
    -- wins, and the reevaluation RPC evaluates denial before allowance).
    -- The contact row is already locked above (v_contact FOR UPDATE), which
    -- serializes this write with any concurrent opt-out via the
    -- contact_authorizations_serialize_contact trigger.
    IF NOT EXISTS (
        SELECT 1
        FROM public.contact_authorizations ca
        WHERE ca.contact_id = p_contact_id
          AND ca.channel = 'whatsapp'
          AND ca.purpose = 'cart_recovery'
          AND ca.valid_from <= v_now
          AND (ca.valid_until IS NULL OR ca.valid_until > v_now)
    ) THEN
        INSERT INTO public.contact_authorizations (
            contact_id,
            channel,
            purpose,
            authorization_status,
            authorization_source,
            evidence,
            valid_from
        ) VALUES (
            p_contact_id,
            'whatsapp',
            'cart_recovery',
            'allowed',
            'hotmart',
            jsonb_build_object(
                'reason', 'cart_abandonment',
                'webhook_event_id', p_webhook_event_id,
                'recovery_case_id', v_case.id
            ),
            v_now
        );
    END IF;

    RETURN QUERY
    SELECT
        v_plan.recovery_case_id::uuid,
        v_plan.followup_sequence_id::uuid,
        v_plan.scheduled_action_id::uuid,
        v_plan.created::boolean;
END;
$function$;

REVOKE EXECUTE ON FUNCTION public.plan_cart_recovery_with_identity(
    uuid, uuid, text, text, text, text, integer, timestamptz, bigint, bigint, text
) FROM PUBLIC;

DO $roles$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE EXECUTE ON FUNCTION public.plan_cart_recovery_with_identity(
            uuid, uuid, text, text, text, text, integer, timestamptz,
            bigint, bigint, text
        ) FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE EXECUTE ON FUNCTION public.plan_cart_recovery_with_identity(
            uuid, uuid, text, text, text, text, integer, timestamptz,
            bigint, bigint, text
        ) FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION public.plan_cart_recovery_with_identity(
            uuid, uuid, text, text, text, text, integer, timestamptz,
            bigint, bigint, text
        ) TO service_role;
    END IF;
END;
$roles$;

COMMIT;
