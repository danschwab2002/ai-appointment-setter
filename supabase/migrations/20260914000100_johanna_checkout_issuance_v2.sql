-- Johanna checkout issuance V2: server-owned offer, opaque Hermes SCK and
-- durable inbound/precheckout attribution. This migration does not enable sends.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

create table public.checkout_offer_catalog (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9_-]{1,100}$'),
    funnel_ref text not null check (funnel_ref ~ '^[a-z0-9_-]{1,100}$'),
    scope_key text not null check (scope_key ~ '^[a-z0-9_-]{1,120}$'),
    scope_version integer not null check (scope_version > 0),
    product_ref text not null check (product_ref ~ '^[A-Za-z0-9_-]{1,128}$'),
    landing_ref text not null check (landing_ref ~ '^[a-z0-9_-]{1,100}$'),
    offer_code text not null check (offer_code ~ '^[A-Za-z0-9_-]{1,128}$'),
    checkout_base_url text not null,
    checkout_mode integer not null check (checkout_mode > 0),
    default_for_inbound boolean not null default false,
    status text not null check (status in ('active', 'retired')),
    version integer not null check (version > 0),
    approved_by text not null check (length(btrim(approved_by)) > 0),
    approved_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp(),
    unique (
        tenant_ref, scope_key, scope_version, product_ref, offer_code, version
    ),
    check (checkout_base_url = 'https://pay.hotmart.com/' || product_ref)
);

create unique index checkout_offer_catalog_one_active_default_idx
on public.checkout_offer_catalog (
    tenant_ref, scope_key, scope_version, lower(product_ref)
)
where status = 'active' and default_for_inbound;

insert into public.checkout_offer_catalog (
    tenant_ref, funnel_ref, scope_key, scope_version,
    product_ref, landing_ref, offer_code,
    checkout_base_url, checkout_mode, default_for_inbound, status,
    version, approved_by, approved_at
) values (
    'lancemos', 'psicologajohanna', 'libre-de-ansiedad-inbound', 2,
    'F106691755G', 'ads-a', 'bxjge6zq',
    'https://pay.hotmart.com/F106691755G', 10, true, 'active',
    1, 'dan-schwab', '2026-09-14T22:11:14+00:00'::timestamptz
);

create table public.checkout_link_issuances (
    id uuid primary key default gen_random_uuid(),
    issuance_ulid text not null unique
        check (issuance_ulid ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'),
    commercial_case_id uuid not null
        references public.commercial_cases(id) on delete restrict,
    purchase_intent_id uuid not null
        references public.purchase_intents(id) on delete restrict,
    contact_id uuid not null references public.contacts(id) on delete restrict,
    channel_identity_id uuid not null
        references public.channel_identities(id) on delete restrict,
    offer_catalog_id uuid not null
        references public.checkout_offer_catalog(id) on delete restrict,
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    chatwoot_conversation_id bigint not null check (chatwoot_conversation_id > 0),
    trigger_external_message_id text not null
        check (trigger_external_message_id ~ '^[1-9][0-9]*$'),
    source_kind text not null
        check (source_kind in ('inbound_request', 'precheckout_request')),
    source_submission_id uuid
        references public.precheckout_submissions(id) on delete restrict,
    original_sck text,
    source_value text not null check (source_value = 'hermes'),
    sck_format_version text not null check (sck_format_version = 'v1'),
    sck_value text not null unique,
    checkout_url_final text not null,
    status text not null default 'reserved'
        check (status in (
            'reserved', 'request_started', 'accepted_by_chatwoot',
            'delivery_unknown', 'purchase_matched'
        )),
    chatwoot_message_id bigint,
    hotmart_webhook_event_id uuid unique
        references public.webhook_events(id) on delete restrict,
    failure_code text,
    created_at timestamptz not null default clock_timestamp(),
    finalized_at timestamptz,
    purchased_at timestamptz,
    unique (
        chatwoot_account_id, chatwoot_inbox_id,
        chatwoot_conversation_id, trigger_external_message_id
    ),
    check (sck_value = 'hermes|v1|' || issuance_ulid),
    check (
        (source_kind = 'inbound_request' and source_submission_id is null)
        or (source_kind = 'precheckout_request' and source_submission_id is not null)
    ),
    check (original_sck is null or length(original_sck) between 1 and 2048),
    check (
        (status in ('reserved', 'request_started') and chatwoot_message_id is null
            and failure_code is null and finalized_at is null
            and hotmart_webhook_event_id is null and purchased_at is null)
        or (status = 'accepted_by_chatwoot' and chatwoot_message_id > 0
            and failure_code is null and finalized_at is not null
            and hotmart_webhook_event_id is null and purchased_at is null)
        or (status = 'delivery_unknown' and chatwoot_message_id is null
            and nullif(btrim(failure_code), '') is not null
            and finalized_at is not null and hotmart_webhook_event_id is null
            and purchased_at is null)
        or (status = 'purchase_matched' and hotmart_webhook_event_id is not null
            and purchased_at is not null)
    )
);

-- This separate constraint makes the exact URL contract easy to inventory.
alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_url_shape check (
        checkout_url_final ~ '^https://pay[.]hotmart[.]com/[A-Za-z0-9_-]+[?]off=[A-Za-z0-9_-]+&checkoutMode=[1-9][0-9]*&src=hermes&sck=hermes%7Cv1%7C[0-7][0-9A-HJKMNP-TV-Z]{25}$'
    );

alter table public.checkout_offer_catalog enable row level security;
alter table public.checkout_link_issuances enable row level security;

create function public.protect_checkout_offer_catalog()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception 'checkout_offer_catalog_append_only'
            using errcode = 'check_violation';
    end if;

    if old.status = 'active'
       and new.status = 'retired'
       and to_jsonb(new) - 'status' = to_jsonb(old) - 'status'
    then
        return new;
    end if;

    raise exception 'checkout_offer_catalog_immutable'
        using errcode = 'check_violation';
end;
$function$;

create trigger checkout_offer_catalog_immutable
before update or delete on public.checkout_offer_catalog
for each row execute function public.protect_checkout_offer_catalog();

create function public.protect_checkout_link_issuance()
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

create trigger checkout_link_issuance_immutable
before update or delete on public.checkout_link_issuances
for each row execute function public.protect_checkout_link_issuance();

create function public.reserve_chatwoot_checkout_issuance_v2(
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
    v_offer public.checkout_offer_catalog%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_issuance public.checkout_link_issuances%rowtype;
    v_source_kind text;
    v_original_sck text;
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

    select offer.* into v_offer
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

    if exists (
        select 1 from public.purchase_intents intent
        where intent.tenant_ref = v_case.tenant_ref
          and intent.funnel_ref = v_offer.funnel_ref
          and intent.landing_ref = v_offer.landing_ref
          and lower(intent.product_ref) = lower(v_offer.product_ref)
          and intent.offer_ref = v_offer.offer_code
          and intent.normalized_phone = p_external_user_id
          and intent.lifecycle_state = 'purchased'
    ) then
        return query select 'purchase_already_approved'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
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
        checkout_url_final
    ) values (
        p_issuance_ulid, v_case.id, v_intent.id,
        v_case.contact_id, v_case.selected_channel_identity_id, v_offer.id,
        p_chatwoot_account_id, p_chatwoot_inbox_id, p_chatwoot_conversation_id,
        p_trigger_external_message_id, v_source_kind, v_submission.id,
        v_original_sck, 'hermes', 'v1', 'hermes|v1|' || p_issuance_ulid,
        v_url
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

create function public.authorize_chatwoot_checkout_issuance_v2(
    p_issuance_id uuid,
    p_external_user_id text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_trigger_external_message_id text,
    p_now timestamptz default clock_timestamp()
)
returns table (outcome text, issuance_id uuid, status text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_issuance public.checkout_link_issuances%rowtype;
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_conversation public.conversations%rowtype;
    v_identity public.channel_identities%rowtype;
    v_intent public.purchase_intents%rowtype;
begin
    if p_issuance_id is null
       or p_external_user_id is null or p_external_user_id !~ '^[1-9][0-9]{7,14}$'
       or p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id <= 0
       or p_trigger_external_message_id is null
       or p_trigger_external_message_id !~ '^[1-9][0-9]*$'
       or p_now is null then
        return query select 'invalid_request'::text, null::uuid, null::text;
        return;
    end if;

    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.id = p_issuance_id
    for update;
    if not found
       or v_issuance.chatwoot_account_id <> p_chatwoot_account_id
       or v_issuance.chatwoot_inbox_id <> p_chatwoot_inbox_id
       or v_issuance.chatwoot_conversation_id <> p_chatwoot_conversation_id
       or v_issuance.trigger_external_message_id <> p_trigger_external_message_id then
        return query select 'invalid_request'::text, null::uuid, null::text;
        return;
    end if;

    if v_issuance.status <> 'reserved' then
        return query select
            case
                when v_issuance.status = 'request_started' then 'request_started_replay'
                when v_issuance.status = 'accepted_by_chatwoot' then 'already_accepted'
                when v_issuance.status = 'delivery_unknown' then 'delivery_unknown'
                else 'purchase_already_approved'
            end,
            v_issuance.id, v_issuance.status;
        return;
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.id = v_issuance.commercial_case_id
      and commercial_case.case_kind = 'inbound_sales'
    for update;
    if not found or v_case.status <> 'active'
       or v_case.automation_status not in ('draft_only', 'enabled') then
        return query select 'blocked_case'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    if not exists (
        select 1
        from public.inbound_commercial_scope_versions scope
        where scope.scope_key = v_case.inbound_scope_key
          and scope.version = v_case.inbound_scope_version
          and scope.status = 'published'
          and scope.tenant_key = v_case.tenant_ref
          and scope.chatwoot_account_id = p_chatwoot_account_id
          and scope.chatwoot_inbox_id = p_chatwoot_inbox_id
          and lower(scope.external_product_id) = lower(v_case.product_ref)
    ) then
        return query select 'blocked_scope'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact
    where contact.id = v_issuance.contact_id
    for update;
    if not found
       or v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or v_contact.lifecycle_status = 'do_not_contact' then
        return query select 'blocked_contact'::text, v_issuance.id, v_issuance.status;
        return;
    end if;
    if public.has_chatwoot_opt_out_stop(
        p_chatwoot_account_id, p_chatwoot_inbox_id,
        p_chatwoot_conversation_id, p_external_user_id
    ) then
        return query select 'blocked_opt_out'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.id = v_case.conversation_id
      and conversation.contact_id = v_issuance.contact_id
      and conversation.channel_identity_id = v_issuance.channel_identity_id
    for update;
    if not found or v_conversation.human_takeover
       or v_conversation.status in ('snoozed', 'paused_human', 'completed', 'closed', 'blocked')
       or v_conversation.automation_status not in ('draft_only', 'enabled')
       or v_conversation.commercial_context #>> '{chatwoot_conversation_id}'
            <> p_chatwoot_conversation_id::text then
        return query select 'blocked_conversation'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_issuance.channel_identity_id
      and identity.external_user_id = p_external_user_id
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and identity.external_conversation_id = p_chatwoot_conversation_id::text
      and identity.identity_status = 'active'
    for update;
    if not found then
        return query select 'blocked_identity'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = v_issuance.purchase_intent_id
    for update;
    if found and v_intent.lifecycle_state = 'purchased' then
        return query select 'purchase_already_approved'::text,
            v_issuance.id, v_issuance.status;
        return;
    end if;
    if not found or v_intent.lifecycle_state <> 'waiting_for_purchase' then
        return query select 'blocked_intent'::text,
            v_issuance.id, v_issuance.status;
        return;
    end if;

    update public.checkout_link_issuances issuance
    set status = 'request_started'
    where issuance.id = v_issuance.id
      and issuance.status = 'reserved'
    returning issuance.* into strict v_issuance;

    return query select 'request_started'::text, v_issuance.id, v_issuance.status;
end;
$function$;

create function public.finalize_chatwoot_checkout_issuance_v2(
    p_issuance_id uuid,
    p_status text,
    p_chatwoot_message_id bigint default null,
    p_failure_code text default null,
    p_now timestamptz default clock_timestamp()
)
returns table (outcome text, issuance_id uuid, status text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_issuance public.checkout_link_issuances%rowtype;
begin
    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.id = p_issuance_id
    for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'checkout_issuance_not_found';
    end if;
    if v_issuance.status = 'delivery_unknown'
       and p_status = 'accepted_by_chatwoot'
       and p_chatwoot_message_id is not null and p_chatwoot_message_id > 0
       and p_failure_code is null then
        update public.checkout_link_issuances
        set status = p_status, chatwoot_message_id = p_chatwoot_message_id,
            failure_code = null, finalized_at = p_now
        where id = v_issuance.id;
        return query select 'finalized'::text, v_issuance.id, p_status;
        return;
    end if;
    if v_issuance.status <> 'request_started' then
        return query select 'already_finalized'::text, v_issuance.id, v_issuance.status;
        return;
    end if;
    if p_status = 'accepted_by_chatwoot'
       and p_chatwoot_message_id is not null and p_chatwoot_message_id > 0
       and p_failure_code is null then
        update public.checkout_link_issuances
        set status = p_status, chatwoot_message_id = p_chatwoot_message_id,
            finalized_at = p_now
        where id = v_issuance.id;
    elsif p_status = 'delivery_unknown'
       and p_chatwoot_message_id is null
       and nullif(btrim(p_failure_code), '') is not null then
        update public.checkout_link_issuances
        set status = p_status, failure_code = p_failure_code,
            finalized_at = p_now
        where id = v_issuance.id;
    else
        raise exception using errcode = '22023', message = 'checkout_issuance_finalize_invalid';
    end if;
    return query select 'finalized'::text, v_issuance.id, p_status;
end;
$function$;

create function public.correlate_hotmart_checkout_issuance_v2(
    p_hotmart_webhook_event_id uuid,
    p_sck_value text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    issuance_id uuid,
    purchase_intent_id uuid,
    commercial_case_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_issuance public.checkout_link_issuances%rowtype;
begin
    if p_hotmart_webhook_event_id is null or p_now is null
       or p_sck_value is null
       or p_sck_value !~ '^hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$' then
        return query select 'invalid_hermes_sck'::text,
            null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.sck_value = p_sck_value
    for update;
    if not found then
        return query select 'not_found'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;
    if v_issuance.hotmart_webhook_event_id = p_hotmart_webhook_event_id then
        return query select 'replay'::text, v_issuance.id,
            v_issuance.purchase_intent_id, v_issuance.commercial_case_id;
        return;
    end if;
    if v_issuance.hotmart_webhook_event_id is not null then
        return query select 'conflict'::text, v_issuance.id,
            v_issuance.purchase_intent_id, v_issuance.commercial_case_id;
        return;
    end if;
    if not exists (
        select 1 from public.webhook_events event
        where event.id = p_hotmart_webhook_event_id
          and event.source = 'hotmart'
          and event.event_type = 'PURCHASE_APPROVED'
    ) then
        return query select 'invalid_purchase_event'::text,
            null::uuid, null::uuid, null::uuid;
        return;
    end if;

    perform 1
    from public.purchase_intents intent
    where intent.id = v_issuance.purchase_intent_id
      and intent.lifecycle_state = 'waiting_for_purchase'
    for update;
    if not found then
        return query select 'purchase_already_approved'::text,
            v_issuance.id, v_issuance.purchase_intent_id,
            v_issuance.commercial_case_id;
        return;
    end if;

    update public.purchase_intents
    set lifecycle_state = 'purchased', current_classification = null,
        activation_authorized = false, updated_at = p_now
    where id = v_issuance.purchase_intent_id
      and lifecycle_state = 'waiting_for_purchase';
    if not found then
        raise exception using errcode = '40001',
            message = 'checkout_issuance_purchase_intent_changed_concurrently';
    end if;

    perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(
        v_issuance.purchase_intent_id, p_now
    );

    update public.checkout_link_issuances
    set status = 'purchase_matched',
        hotmart_webhook_event_id = p_hotmart_webhook_event_id,
        purchased_at = p_now
    where id = v_issuance.id;

    return query select 'matched'::text, v_issuance.id,
        v_issuance.purchase_intent_id, v_issuance.commercial_case_id;
end;
$function$;

create function public.admit_and_correlate_hotmart_checkout_issuance_v2(
    p_external_event_id text,
    p_payload jsonb,
    p_sck_value text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    admission_outcome text,
    webhook_event_id uuid,
    correlation_outcome text,
    issuance_id uuid,
    purchase_intent_id uuid,
    commercial_case_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_admission_outcome text;
    v_webhook_event_id uuid;
    v_correlation_outcome text;
    v_issuance_id uuid;
    v_purchase_intent_id uuid;
    v_commercial_case_id uuid;
begin
    if p_external_event_id is null or nullif(btrim(p_external_event_id), '') is null
       or p_payload is null or p_now is null
       or p_sck_value is null
       or p_payload #>> '{data,purchase,origin,sck}' is distinct from p_sck_value
       or p_sck_value !~ '^hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$' then
        raise exception using errcode = '22023',
            message = 'hotmart_checkout_issuance_admission_invalid';
    end if;

    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_webhook_event_id
    from public._admit_hotmart_purchase_approved_base(
        p_external_event_id, p_payload
    ) admission;

    if v_admission_outcome = 'semantic_conflict' then
        return query select v_admission_outcome, v_webhook_event_id,
            'semantic_conflict'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select correlation.outcome, correlation.issuance_id,
           correlation.purchase_intent_id, correlation.commercial_case_id
    into strict v_correlation_outcome, v_issuance_id,
         v_purchase_intent_id, v_commercial_case_id
    from public.correlate_hotmart_checkout_issuance_v2(
        v_webhook_event_id, p_sck_value, p_now
    ) correlation;

    return query select v_admission_outcome, v_webhook_event_id,
        v_correlation_outcome, v_issuance_id,
        v_purchase_intent_id, v_commercial_case_id;
end;
$function$;

revoke all on table public.checkout_offer_catalog from public;
revoke all on table public.checkout_link_issuances from public;
revoke all on function public.protect_checkout_offer_catalog() from public;
revoke all on function public.protect_checkout_link_issuance() from public;
revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from public;
revoke all on function public.authorize_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, timestamptz) from public;
revoke all on function public.finalize_chatwoot_checkout_issuance_v2(uuid, text, bigint, text, timestamptz) from public;
revoke all on function public.correlate_hotmart_checkout_issuance_v2(uuid, text, timestamptz) from public;
revoke all on function public.admit_and_correlate_hotmart_checkout_issuance_v2(text, jsonb, text, timestamptz) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on table public.checkout_offer_catalog from anon';
        execute 'revoke all on table public.checkout_link_issuances from anon';
        execute 'revoke all on function public.protect_checkout_offer_catalog() from anon';
        execute 'revoke all on function public.protect_checkout_link_issuance() from anon';
        execute 'revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from anon';
        execute 'revoke all on function public.authorize_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, timestamptz) from anon';
        execute 'revoke all on function public.finalize_chatwoot_checkout_issuance_v2(uuid, text, bigint, text, timestamptz) from anon';
        execute 'revoke all on function public.correlate_hotmart_checkout_issuance_v2(uuid, text, timestamptz) from anon';
        execute 'revoke all on function public.admit_and_correlate_hotmart_checkout_issuance_v2(text, jsonb, text, timestamptz) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on table public.checkout_offer_catalog from authenticated';
        execute 'revoke all on table public.checkout_link_issuances from authenticated';
        execute 'revoke all on function public.protect_checkout_offer_catalog() from authenticated';
        execute 'revoke all on function public.protect_checkout_link_issuance() from authenticated';
        execute 'revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from authenticated';
        execute 'revoke all on function public.authorize_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, timestamptz) from authenticated';
        execute 'revoke all on function public.finalize_chatwoot_checkout_issuance_v2(uuid, text, bigint, text, timestamptz) from authenticated';
        execute 'revoke all on function public.correlate_hotmart_checkout_issuance_v2(uuid, text, timestamptz) from authenticated';
        execute 'revoke all on function public.admit_and_correlate_hotmart_checkout_issuance_v2(text, jsonb, text, timestamptz) from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke all on table public.checkout_offer_catalog from service_role';
        execute 'revoke all on table public.checkout_link_issuances from service_role';
        execute 'revoke all on function public.protect_checkout_offer_catalog() from service_role';
        execute 'revoke all on function public.protect_checkout_link_issuance() from service_role';
        execute 'revoke all on function public.correlate_hotmart_checkout_issuance_v2(uuid, text, timestamptz) from service_role';
        execute 'grant execute on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) to service_role';
        execute 'grant execute on function public.authorize_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, timestamptz) to service_role';
        execute 'grant execute on function public.finalize_chatwoot_checkout_issuance_v2(uuid, text, bigint, text, timestamptz) to service_role';
        execute 'grant execute on function public.admit_and_correlate_hotmart_checkout_issuance_v2(text, jsonb, text, timestamptz) to service_role';
    end if;
end;
$roles$;

commit;
