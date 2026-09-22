-- Johanna checkout offer by lead intent V1: the recuperador link carries the
-- offer the lead already saw on the landing. The offer is resolved from the
-- lead's latest live purchase intent of the funnel (real precheckout intents
-- first), and falls back to the catalog default when there is no intent or
-- the intent's offer is not in the catalog. The resolution is recorded on the
-- issuance row. Prospective only: existing issuances keep their URL.
--
-- Decision: Dan Schwab, 2026-09-22 ("la persona que llego por una determinada
-- oferta debe recibir la URL que la vuelva a llevar a esa misma oferta").
-- Data: the six landing/offer pairs already published and validated by the
-- precheckout admission (public.johanna_precheckout_landing_offers, 2026-08-31).

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- 1. The issuance row says how the offer was resolved.
--    'catalog_default' is the value of rows issued before this migration.
alter table public.checkout_link_issuances
    add column offer_resolution text not null default 'catalog_default'
        constraint checkout_link_issuances_offer_resolution_check check (
            offer_resolution in (
                'lead_intent',
                'default_no_intent',
                'default_offer_not_in_catalog',
                'catalog_default'
            )
        ),
    add column lead_offer_code text
        constraint checkout_link_issuances_lead_offer_code_check check (
            lead_offer_code is null or lead_offer_code ~ '^[A-Za-z0-9_-]{1,128}$'
        );

-- 2. The resolution is immutable like the rest of the issuance identity.
create or replace function public.protect_checkout_link_issuance()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE'
       or old.id is distinct from new.id
       or old.issuance_ulid is distinct from new.issuance_ulid
       or old.commercial_case_id is distinct from new.commercial_case_id
       or old.purchase_intent_id is distinct from new.purchase_intent_id
       or old.contact_id is distinct from new.contact_id
       or old.channel_identity_id is distinct from new.channel_identity_id
       or old.offer_catalog_id is distinct from new.offer_catalog_id
       or old.chatwoot_account_id is distinct from new.chatwoot_account_id
       or old.chatwoot_inbox_id is distinct from new.chatwoot_inbox_id
       or old.chatwoot_conversation_id is distinct from new.chatwoot_conversation_id
       or old.trigger_external_message_id is distinct from new.trigger_external_message_id
       or old.source_kind is distinct from new.source_kind
       or old.source_submission_id is distinct from new.source_submission_id
       or old.original_sck is distinct from new.original_sck
       or old.source_value is distinct from new.source_value
       or old.sck_format_version is distinct from new.sck_format_version
       or old.sck_value is distinct from new.sck_value
       or old.checkout_url_final is distinct from new.checkout_url_final
       or old.offer_resolution is distinct from new.offer_resolution
       or old.lead_offer_code is distinct from new.lead_offer_code
       or old.created_at is distinct from new.created_at
       or not (
            (old.status = 'reserved'
                and new.status in ('request_started', 'purchase_matched'))
            or (old.status = 'request_started'
                and new.status in ('accepted_by_chatwoot', 'delivery_unknown', 'purchase_matched'))
            or (old.status = 'delivery_unknown'
                and new.status in ('accepted_by_chatwoot', 'purchase_matched'))
            or (old.status = 'accepted_by_chatwoot'
                and new.status = 'purchase_matched')
       ) then
        raise exception using errcode = '55000', message = 'checkout_link_issuance_immutable';
    end if;
    return new;
end;
$function$;

-- 3. The catalog mirrors the six published landing/offer pairs. ads-a keeps
--    the default; the other five are active and never default. Codes are read
--    from the published pairs, not typed again: the link must repeat exactly
--    what the landing sent the person to.
do $preflight_pairs$
begin
    if (select count(*) from public.johanna_precheckout_landing_offers) <> 6 then
        raise exception using errcode = '55000',
            message = 'johanna_precheckout_landing_offers_expected_six';
    end if;
end;
$preflight_pairs$;

insert into public.checkout_offer_catalog (
    tenant_ref, funnel_ref, scope_key, scope_version,
    product_ref, landing_ref, offer_code,
    checkout_base_url, checkout_mode, default_for_inbound, status,
    version, approved_by, approved_at
)
select 'lancemos', 'psicologajohanna', 'libre-de-ansiedad-inbound', 2,
       'F106691755G', pair.landing_ref, pair.offer_ref,
       'https://pay.hotmart.com/F106691755G', 10, false, 'active',
       1, 'dan-schwab', '2026-09-22T12:30:00+00:00'::timestamptz
from public.johanna_precheckout_landing_offers pair
where not exists (
    select 1 from public.checkout_offer_catalog existing
    where existing.tenant_ref = 'lancemos'
      and existing.scope_key = 'libre-de-ansiedad-inbound'
      and existing.scope_version = 2
      and lower(existing.product_ref) = lower('F106691755G')
      and existing.offer_code = pair.offer_ref
      and existing.status = 'active'
)
order by pair.landing_ref;

do $postflight_catalog$
begin
    if (
        select count(*) from public.checkout_offer_catalog offer
        where offer.tenant_ref = 'lancemos'
          and offer.scope_key = 'libre-de-ansiedad-inbound'
          and offer.scope_version = 2
          and lower(offer.product_ref) = lower('F106691755G')
          and offer.status = 'active'
    ) <> 6 or (
        select count(*) from public.checkout_offer_catalog offer
        where offer.tenant_ref = 'lancemos'
          and offer.scope_key = 'libre-de-ansiedad-inbound'
          and offer.scope_version = 2
          and lower(offer.product_ref) = lower('F106691755G')
          and offer.status = 'active'
          and offer.default_for_inbound
          and offer.landing_ref = 'ads-a'
          and offer.offer_code = 'bxjge6zq'
    ) <> 1 then
        raise exception using errcode = '55000',
            message = 'johanna_checkout_offer_catalog_expected_six_with_ads_a_default';
    end if;
end;
$postflight_catalog$;

-- 4. Same RPC name and signature as V2 (the bridge does not change). What
--    changes: the offer comes from the lead's intent, the known-purchase guard
--    covers every offer of the product, and the row records the resolution.
create or replace function public.reserve_chatwoot_checkout_issuance_v2(
    p_commercial_case_id uuid,
    p_external_user_id text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_trigger_external_message_id text,
    p_issuance_ulid text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    issuance_id uuid,
    issuance_ulid text,
    purchase_intent_id uuid,
    source_kind text,
    checkout_url_final text,
    source_value text,
    sck_value text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_conversation public.conversations%rowtype;
    v_identity public.channel_identities%rowtype;
    v_scope public.inbound_commercial_scope_versions%rowtype;
    v_default_offer public.checkout_offer_catalog%rowtype;
    v_offer public.checkout_offer_catalog%rowtype;
    v_lead_intent public.purchase_intents%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_issuance public.checkout_link_issuances%rowtype;
    v_source_kind text;
    v_original_sck text;
    v_offer_resolution text;
    v_lead_offer_code text;
    v_url text;
begin
    if p_external_user_id is null or p_external_user_id !~ '^[1-9][0-9]{7,14}$'
       or p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id <= 0
       or p_trigger_external_message_id is null
       or p_trigger_external_message_id !~ '^[1-9][0-9]*$'
       or p_issuance_ulid is null
       or p_issuance_ulid !~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
       or p_now is null then
        return query select 'invalid_request'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        p_chatwoot_account_id::text || ':' || p_chatwoot_inbox_id::text || ':' ||
        p_chatwoot_conversation_id::text || ':' || p_trigger_external_message_id,
        0
    ));

    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.chatwoot_account_id = p_chatwoot_account_id
      and issuance.chatwoot_inbox_id = p_chatwoot_inbox_id
      and issuance.chatwoot_conversation_id = p_chatwoot_conversation_id
      and issuance.trigger_external_message_id = p_trigger_external_message_id
    for update;
    if found then
        if v_issuance.commercial_case_id is distinct from p_commercial_case_id
           or not exists (
                select 1
                from public.commercial_cases replay_case
                join public.channel_identities replay_identity
                  on replay_identity.id = replay_case.selected_channel_identity_id
                where replay_case.id = p_commercial_case_id
                  and replay_case.contact_id = v_issuance.contact_id
                  and replay_case.selected_channel_identity_id = v_issuance.channel_identity_id
                  and replay_identity.external_user_id = p_external_user_id
                  and replay_identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
                  and replay_identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
                  and replay_identity.external_conversation_id = p_chatwoot_conversation_id::text
           ) then
            return query select 'replay_conflict'::text, null::uuid, null::text,
                null::uuid, null::text, null::text, null::text, null::text;
            return;
        end if;
        return query select
            case
                when v_issuance.status = 'accepted_by_chatwoot' then 'already_accepted'
                when v_issuance.status = 'delivery_unknown' then 'delivery_unknown'
                when v_issuance.status = 'purchase_matched' then 'purchase_already_approved'
                when v_issuance.status = 'request_started' then 'request_started_replay'
                else 'reserved'
            end,
            v_issuance.id, v_issuance.issuance_ulid,
            v_issuance.purchase_intent_id, v_issuance.source_kind,
            v_issuance.checkout_url_final, v_issuance.source_value,
            v_issuance.sck_value;
        return;
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.id = p_commercial_case_id
      and commercial_case.case_kind = 'inbound_sales'
    for update;
    if not found or v_case.status <> 'active'
       or v_case.automation_status not in ('draft_only', 'enabled') then
        return query select 'blocked_case'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select scope.* into v_scope
    from public.inbound_commercial_scope_versions scope
    where scope.scope_key = v_case.inbound_scope_key
      and scope.version = v_case.inbound_scope_version
      and scope.status = 'published'
      and scope.tenant_key = v_case.tenant_ref
      and scope.chatwoot_account_id = p_chatwoot_account_id
      and scope.chatwoot_inbox_id = p_chatwoot_inbox_id
      and lower(scope.external_product_id) = lower(v_case.product_ref)
    for share;
    if not found then
        return query select 'blocked_scope'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select offer.* into v_default_offer
    from public.checkout_offer_catalog offer
    where offer.tenant_ref = v_case.tenant_ref
      and offer.scope_key = v_case.inbound_scope_key
      and offer.scope_version = v_case.inbound_scope_version
      and lower(offer.product_ref) = lower(v_case.product_ref)
      and offer.status = 'active'
      and offer.default_for_inbound
    for share;
    if not found then
        return query select 'missing_default_offer'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact where contact.id = v_case.contact_id
    for update;
    if not found
       or v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or v_contact.lifecycle_status = 'do_not_contact' then
        return query select 'blocked_contact'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;
    if public.has_chatwoot_opt_out_stop(
        p_chatwoot_account_id, p_chatwoot_inbox_id,
        p_chatwoot_conversation_id, p_external_user_id
    ) then
        return query select 'blocked_opt_out'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.id = v_case.conversation_id
      and conversation.contact_id = v_case.contact_id
      and conversation.channel_identity_id = v_case.selected_channel_identity_id
    for update;
    if not found or v_conversation.human_takeover
       or v_conversation.status in (
            'snoozed', 'paused_human', 'completed', 'closed', 'blocked'
       )
       or v_conversation.automation_status not in ('draft_only', 'enabled')
       or v_conversation.commercial_context #>> '{chatwoot_conversation_id}'
            <> p_chatwoot_conversation_id::text then
        return query select 'blocked_conversation'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.external_user_id = p_external_user_id
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and identity.external_conversation_id = p_chatwoot_conversation_id::text
      and identity.identity_status = 'active'
    for update;
    if not found then
        return query select 'blocked_identity'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    -- A known purchase of the product blocks, whichever offer it came through.
    -- With per-lead offers a per-offer guard would let a buyer receive a second
    -- link just by having seen another landing.
    if exists (
        select 1 from public.purchase_intents intent
        where intent.tenant_ref = v_case.tenant_ref
          and intent.funnel_ref = v_default_offer.funnel_ref
          and lower(intent.product_ref) = lower(v_default_offer.product_ref)
          and intent.normalized_phone = p_external_user_id
          and intent.lifecycle_state = 'purchased'
    ) then
        return query select 'purchase_already_approved'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    -- The lead's offer: the latest live intent of this phone in the funnel.
    -- Intents backed by a real precheckout submission win over intents this
    -- RPC fabricated for earlier inbound links; among equals, the newest.
    select intent.* into v_lead_intent
    from public.purchase_intents intent
    where intent.tenant_ref = v_case.tenant_ref
      and intent.funnel_ref = v_default_offer.funnel_ref
      and lower(intent.product_ref) = lower(v_default_offer.product_ref)
      and intent.normalized_phone = p_external_user_id
      and intent.lifecycle_state = 'waiting_for_purchase'
    order by
        exists (
            select 1 from public.purchase_intent_submissions link
            where link.purchase_intent_id = intent.id
        ) desc,
        intent.submitted_at desc,
        intent.id desc
    limit 1;

    if found then
        v_lead_offer_code := v_lead_intent.offer_ref;
        select offer.* into v_offer
        from public.checkout_offer_catalog offer
        where offer.tenant_ref = v_default_offer.tenant_ref
          and offer.scope_key = v_default_offer.scope_key
          and offer.scope_version = v_default_offer.scope_version
          and lower(offer.product_ref) = lower(v_default_offer.product_ref)
          and offer.landing_ref = v_lead_intent.landing_ref
          and offer.offer_code = v_lead_intent.offer_ref
          and offer.status = 'active'
        order by offer.version desc
        limit 1
        for share;
        if found then
            v_offer_resolution := 'lead_intent';
        else
            v_offer := v_default_offer;
            v_offer_resolution := 'default_offer_not_in_catalog';
        end if;
    else
        v_offer := v_default_offer;
        v_offer_resolution := 'default_no_intent';
    end if;

    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.tenant_ref = v_case.tenant_ref
      and intent.funnel_ref = v_offer.funnel_ref
      and intent.landing_ref = v_offer.landing_ref
      and lower(intent.product_ref) = lower(v_offer.product_ref)
      and intent.offer_ref = v_offer.offer_code
      and intent.normalized_phone = p_external_user_id
      and intent.lifecycle_state = 'waiting_for_purchase'
    order by intent.submitted_at desc, intent.id desc
    limit 1
    for update;

    if not found then
        insert into public.purchase_intents (
            tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
            normalized_email, normalized_phone, submitted_at, lifecycle_state,
            whatsapp_contact_authorized, provisional, provider_observed,
            activation_authorized
        ) values (
            v_offer.tenant_ref, v_offer.funnel_ref, v_offer.landing_ref,
            v_offer.product_ref, v_offer.offer_code, null, p_external_user_id,
            p_now, 'waiting_for_purchase', true, false, false, true
        ) returning * into v_intent;
    end if;

    select submission.* into v_submission
    from public.purchase_intent_submissions link
    join public.precheckout_submissions submission on submission.id = link.submission_id
    where link.purchase_intent_id = v_intent.id
    order by link.ordinal desc, submission.id desc
    limit 1
    for share of submission;

    if found then
        v_source_kind := 'precheckout_request';
        v_original_sck := nullif(
            btrim(v_submission.raw_payload #>> '{data,attribution,sck}'), ''
        );
    else
        v_source_kind := 'inbound_request';
        v_original_sck := null;
    end if;

    v_url := v_offer.checkout_base_url || '?off=' || v_offer.offer_code
        || '&checkoutMode=' || v_offer.checkout_mode::text
        || '&src=hermes&sck=hermes%7Cv1%7C' || p_issuance_ulid;

    insert into public.checkout_link_issuances (
        issuance_ulid, commercial_case_id, purchase_intent_id,
        contact_id, channel_identity_id, offer_catalog_id,
        chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id,
        trigger_external_message_id, source_kind, source_submission_id,
        original_sck, source_value, sck_format_version, sck_value,
        checkout_url_final, offer_resolution, lead_offer_code
    ) values (
        p_issuance_ulid, v_case.id, v_intent.id,
        v_case.contact_id, v_case.selected_channel_identity_id, v_offer.id,
        p_chatwoot_account_id, p_chatwoot_inbox_id, p_chatwoot_conversation_id,
        p_trigger_external_message_id, v_source_kind, v_submission.id,
        v_original_sck, 'hermes', 'v1', 'hermes|v1|' || p_issuance_ulid,
        v_url, v_offer_resolution, v_lead_offer_code
    ) on conflict (
        chatwoot_account_id, chatwoot_inbox_id,
        chatwoot_conversation_id, trigger_external_message_id
    ) do nothing
    returning * into v_issuance;

    if v_issuance.id is null then
        select issuance.* into strict v_issuance
        from public.checkout_link_issuances issuance
        where issuance.chatwoot_account_id = p_chatwoot_account_id
          and issuance.chatwoot_inbox_id = p_chatwoot_inbox_id
          and issuance.chatwoot_conversation_id = p_chatwoot_conversation_id
          and issuance.trigger_external_message_id = p_trigger_external_message_id
        for update;
        return query select 'reserved'::text,
            v_issuance.id, v_issuance.issuance_ulid,
            v_issuance.purchase_intent_id, v_issuance.source_kind,
            v_issuance.checkout_url_final, v_issuance.source_value,
            v_issuance.sck_value;
        return;
    end if;

    return query select 'reserved'::text,
        v_issuance.id, v_issuance.issuance_ulid,
        v_issuance.purchase_intent_id, v_issuance.source_kind,
        v_issuance.checkout_url_final, v_issuance.source_value,
        v_issuance.sck_value;
end;
$function$;

-- 5. create or replace keeps the ACL of the replaced functions; restated so the
--    grant stays explicit in this file (same as the V2 migration).
revoke all on function public.protect_checkout_link_issuance() from public;
revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on function public.protect_checkout_link_issuance() from anon';
        execute 'revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on function public.protect_checkout_link_issuance() from authenticated';
        execute 'revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke all on function public.protect_checkout_link_issuance() from service_role';
        execute 'grant execute on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) to service_role';
    end if;
end;
$roles$;

commit;
