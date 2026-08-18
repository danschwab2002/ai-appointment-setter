-- Provisional pre-checkout form admission. This migration only records intent;
-- it cannot schedule or send any commercial effect.

begin;

create table public.precheckout_submissions (
    id uuid primary key default gen_random_uuid(),
    external_submission_id text not null unique,
    contract_version text not null,
    raw_payload jsonb not null,
    canonical_payload jsonb not null,
    provisional boolean not null,
    provider_observed boolean not null,
    activation_authorized boolean not null,
    received_at timestamptz not null default now(),
    check (btrim(external_submission_id) <> ''),
    check (btrim(contract_version) <> '')
);

create table public.purchase_intents (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null,
    funnel_ref text not null,
    landing_ref text not null,
    product_ref text not null,
    offer_ref text not null,
    normalized_email text,
    normalized_phone text not null,
    submitted_at timestamptz not null,
    lifecycle_state text not null default 'waiting_for_purchase',
    current_classification text,
    whatsapp_contact_authorized boolean not null,
    provisional boolean not null,
    provider_observed boolean not null,
    activation_authorized boolean not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (lifecycle_state in (
        'waiting_for_purchase', 'purchased', 'cancelled'
    )),
    check (current_classification is null or current_classification in (
        'payment_failure_supported', 'abandonment_candidate',
        'identity_conflict', 'tracking_incomplete', 'expired_unknown'
    )),
    check (normalized_phone ~ '^[1-9][0-9]{7,14}$'),
    check (btrim(normalized_email) = lower(btrim(normalized_email)))
);

create unique index purchase_intents_one_live_identity_idx
on public.purchase_intents (
    tenant_ref,
    funnel_ref,
    product_ref,
    offer_ref,
    normalized_phone
)
where lifecycle_state = 'waiting_for_purchase';

create table public.purchase_intent_submissions (
    purchase_intent_id uuid not null references public.purchase_intents(id),
    submission_id uuid not null unique references public.precheckout_submissions(id),
    ordinal integer not null check (ordinal > 0),
    attached_at timestamptz not null default now(),
    primary key (purchase_intent_id, ordinal)
);

create table public.precheckout_submission_conflicts (
    id uuid primary key default gen_random_uuid(),
    external_submission_id text not null,
    existing_submission_id uuid not null references public.precheckout_submissions(id),
    content_fingerprint text not null,
    incoming_raw_payload jsonb not null,
    incoming_canonical_payload jsonb not null,
    detected_at timestamptz not null default now(),
    resolved_at timestamptz,
    resolution text,
    check (content_fingerprint ~ '^[0-9a-f]{64}$'),
    unique (existing_submission_id, content_fingerprint)
);

create index precheckout_submission_conflicts_unresolved_idx
on public.precheckout_submission_conflicts (external_submission_id, detected_at)
where resolved_at is null;

create or replace function public.admit_precheckout_form_submission(
    p_external_submission_id text,
    p_raw_payload jsonb,
    p_canonical_payload jsonb
)
returns table (
    outcome text,
    submission_id uuid,
    purchase_intent_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_existing public.precheckout_submissions%rowtype;
    v_submission_id uuid;
    v_purchase_intent_id uuid;
    v_submitted_at timestamptz;
    v_email text;
    v_phone text;
    v_content_fingerprint text;
    v_existing_conflict public.precheckout_submission_conflicts%rowtype;
begin
    if p_external_submission_id is null
       or btrim(p_external_submission_id) = ''
       or p_raw_payload is null
       or jsonb_typeof(p_raw_payload) <> 'object'
       or p_canonical_payload is null
       or jsonb_typeof(p_canonical_payload) <> 'object' then
        raise exception using errcode = '22023', message = 'invalid_precheckout_admission_input';
    end if;

    if p_raw_payload #>> '{id}' is distinct from p_external_submission_id
       or p_canonical_payload #>> '{external_submission_id}' is distinct from p_external_submission_id
       or p_canonical_payload #>> '{event_type}' is distinct from 'PRECHECKOUT_FORM_SUBMITTED'
       or p_canonical_payload #>> '{contract_version}' is distinct from '1.0.0-emulated'
       or jsonb_typeof(p_canonical_payload #> '{external_submission_id}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{event_type}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{contract_version}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{submitted_at}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{source,tenant_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{source,funnel_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{source,landing_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{identity,phone}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{lead,full_name}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{commerce,product_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{commerce,offer_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{consent,terms_accepted}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{consent,privacy_accepted}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{consent,whatsapp_contact}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{consent,copy_version}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{assurance,provisional}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{assurance,provider_observed}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{assurance,activation_authorized}') is distinct from 'boolean'
       or p_canonical_payload #>> '{consent,terms_accepted}' is distinct from 'false'
       or p_canonical_payload #>> '{consent,privacy_accepted}' is distinct from 'false'
       or p_canonical_payload #>> '{consent,whatsapp_contact}' is distinct from 'false'
       or p_canonical_payload #>> '{assurance,provisional}' is distinct from 'true'
       or p_canonical_payload #>> '{assurance,provider_observed}' is distinct from 'false'
       or p_canonical_payload #>> '{assurance,activation_authorized}' is distinct from 'false' then
        raise exception using errcode = '22023', message = 'precheckout_payload_assurance_mismatch';
    end if;

    v_email := nullif(lower(btrim(p_canonical_payload #>> '{identity,email}')), '');
    v_phone := nullif(p_canonical_payload #>> '{identity,phone}', '');
    begin
        v_submitted_at := (p_canonical_payload #>> '{submitted_at}')::timestamptz;
    exception when others then
        raise exception using errcode = '22023', message = 'precheckout_invalid_submitted_at';
    end;

    if (v_email is not null and v_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$')
       or v_phone is null
       or v_phone !~ '^[1-9][0-9]{7,14}$'
       or v_submitted_at is null
       or nullif(p_canonical_payload #>> '{lead,full_name}', '') is null
       or nullif(p_canonical_payload #>> '{consent,copy_version}', '') is null
       or nullif(p_canonical_payload #>> '{source,tenant_ref}', '') is null
       or nullif(p_canonical_payload #>> '{source,funnel_ref}', '') is null
       or nullif(p_canonical_payload #>> '{source,landing_ref}', '') is null
       or nullif(p_canonical_payload #>> '{commerce,product_ref}', '') is null
       or nullif(p_canonical_payload #>> '{commerce,offer_ref}', '') is null then
        raise exception using errcode = '22023', message = 'precheckout_invalid_canonical_payload';
    end if;

    v_content_fingerprint := encode(sha256(convert_to(
        p_raw_payload::text || chr(31) || p_canonical_payload::text,
        'UTF8'
    )), 'hex');

    perform pg_advisory_xact_lock(hashtextextended(p_external_submission_id, 0));
    perform pg_advisory_xact_lock(hashtextextended(concat_ws(
        E'\x1f',
        p_canonical_payload #>> '{source,tenant_ref}',
        p_canonical_payload #>> '{source,funnel_ref}',
        p_canonical_payload #>> '{commerce,product_ref}',
        p_canonical_payload #>> '{commerce,offer_ref}',
        v_phone
    ), 0));

    select ps.* into v_existing
    from public.precheckout_submissions ps
    where ps.external_submission_id = p_external_submission_id
    for update;

    if found then
        select pi.id into v_purchase_intent_id
        from public.purchase_intent_submissions pis
        join public.purchase_intents pi on pi.id = pis.purchase_intent_id
        where pis.submission_id = v_existing.id;
        if v_purchase_intent_id is null then
            raise exception using errcode = '55000', message = 'precheckout_submission_without_intent';
        end if;
        if v_existing.raw_payload = p_raw_payload
           and v_existing.canonical_payload = p_canonical_payload then
            return query select 'duplicate'::text, v_existing.id, v_purchase_intent_id;
            return;
        end if;
        select conflict.* into v_existing_conflict
        from public.precheckout_submission_conflicts conflict
        where conflict.existing_submission_id = v_existing.id
          and conflict.content_fingerprint = v_content_fingerprint
        for update;
        if found then
            if v_existing_conflict.incoming_raw_payload is distinct from p_raw_payload
               or v_existing_conflict.incoming_canonical_payload is distinct from p_canonical_payload then
                raise exception using
                    errcode = '55000',
                    message = 'precheckout_conflict_fingerprint_collision';
            end if;
            return query select 'semantic_conflict'::text, v_existing.id, v_purchase_intent_id;
            return;
        end if;
        insert into public.precheckout_submission_conflicts (
            external_submission_id,
            existing_submission_id,
            content_fingerprint,
            incoming_raw_payload,
            incoming_canonical_payload
        ) values (
            p_external_submission_id,
            v_existing.id,
            v_content_fingerprint,
            p_raw_payload,
            p_canonical_payload
        );
        return query select 'semantic_conflict'::text, v_existing.id, v_purchase_intent_id;
        return;
    end if;

    insert into public.precheckout_submissions (
        external_submission_id,
        contract_version,
        raw_payload,
        canonical_payload,
        provisional,
        provider_observed,
        activation_authorized
    ) values (
        p_external_submission_id,
        '1.0.0-emulated',
        p_raw_payload,
        p_canonical_payload,
        true,
        false,
        false
    ) returning id into v_submission_id;

    select pi.id into v_purchase_intent_id
    from public.purchase_intents pi
    where pi.tenant_ref = p_canonical_payload #>> '{source,tenant_ref}'
      and pi.funnel_ref = p_canonical_payload #>> '{source,funnel_ref}'
      and pi.product_ref = p_canonical_payload #>> '{commerce,product_ref}'
      and pi.offer_ref = p_canonical_payload #>> '{commerce,offer_ref}'
      and pi.normalized_phone = v_phone
      and pi.lifecycle_state = 'waiting_for_purchase'
    for update;

    if v_purchase_intent_id is null then
        insert into public.purchase_intents (
            tenant_ref,
            funnel_ref,
            landing_ref,
            product_ref,
            offer_ref,
            normalized_email,
            normalized_phone,
            submitted_at,
            lifecycle_state,
            current_classification,
            whatsapp_contact_authorized,
            provisional,
            provider_observed,
            activation_authorized
        ) values (
            p_canonical_payload #>> '{source,tenant_ref}',
            p_canonical_payload #>> '{source,funnel_ref}',
            p_canonical_payload #>> '{source,landing_ref}',
            p_canonical_payload #>> '{commerce,product_ref}',
            p_canonical_payload #>> '{commerce,offer_ref}',
            v_email,
            v_phone,
            v_submitted_at,
            'waiting_for_purchase',
            null,
            (p_canonical_payload #>> '{consent,whatsapp_contact}')::boolean,
            true,
            false,
            false
        ) returning id into v_purchase_intent_id;
    end if;

    update public.purchase_intents
    set whatsapp_contact_authorized = whatsapp_contact_authorized and
            (p_canonical_payload #>> '{consent,whatsapp_contact}')::boolean,
        updated_at = now()
    where id = v_purchase_intent_id;

    insert into public.purchase_intent_submissions (
        purchase_intent_id,
        submission_id,
        ordinal
    )
    select
        v_purchase_intent_id,
        v_submission_id,
        coalesce(max(pis.ordinal), 0) + 1
    from public.purchase_intent_submissions pis
    where pis.purchase_intent_id = v_purchase_intent_id;

    return query select 'inserted'::text, v_submission_id, v_purchase_intent_id;
end;
$function$;

revoke all on table public.precheckout_submissions from public;
revoke all on table public.purchase_intents from public;
revoke all on table public.purchase_intent_submissions from public;
revoke all on table public.precheckout_submission_conflicts from public;
revoke all on function public.admit_precheckout_form_submission(text, jsonb, jsonb) from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.precheckout_submissions from anon;
        revoke all on table public.purchase_intents from anon;
        revoke all on table public.purchase_intent_submissions from anon;
        revoke all on table public.precheckout_submission_conflicts from anon;
        revoke all on function public.admit_precheckout_form_submission(text, jsonb, jsonb) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.precheckout_submissions from authenticated;
        revoke all on table public.purchase_intents from authenticated;
        revoke all on table public.purchase_intent_submissions from authenticated;
        revoke all on table public.precheckout_submission_conflicts from authenticated;
        revoke all on function public.admit_precheckout_form_submission(text, jsonb, jsonb) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.precheckout_submissions from service_role;
        revoke all on table public.purchase_intents from service_role;
        revoke all on table public.purchase_intent_submissions from service_role;
        revoke all on table public.precheckout_submission_conflicts from service_role;
        grant execute on function public.admit_precheckout_form_submission(text, jsonb, jsonb)
        to service_role;
    end if;
end;
$acl$;

commit;