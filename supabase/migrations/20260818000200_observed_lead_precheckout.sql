-- Admit the observed Lancemos lead.precheckout v1 contract as intent only.
-- This migration grants no contact, activation, planning, or sending authority.

begin;

alter table public.purchase_intents
    alter column normalized_phone drop not null;

alter table public.purchase_intents
    drop constraint if exists purchase_intents_normalized_phone_check;

alter table public.purchase_intents
    add constraint purchase_intents_normalized_phone_check
    check (normalized_phone is null or normalized_phone ~ '^[1-9][0-9]{7,14}$');

create unique index if not exists purchase_intents_one_observed_email_idx
on public.purchase_intents (
    tenant_ref,
    funnel_ref,
    product_ref,
    offer_ref,
    normalized_email
)
where lifecycle_state = 'waiting_for_purchase'
  and provider_observed
  and normalized_email is not null;

create or replace function public.admit_observed_lead_precheckout(
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
    v_email_intent_id uuid;
    v_phone_intent_id uuid;
begin
    if p_external_submission_id is null
       or btrim(p_external_submission_id) = ''
       or p_raw_payload is null
       or jsonb_typeof(p_raw_payload) <> 'object'
       or p_canonical_payload is null
       or jsonb_typeof(p_canonical_payload) <> 'object' then
        raise exception using errcode = '22023', message = 'invalid_observed_precheckout_input';
    end if;

    if p_raw_payload #>> '{id}' is distinct from p_external_submission_id
       or p_raw_payload #>> '{event}' is distinct from 'lead.precheckout'
       or p_raw_payload #>> '{version}' is distinct from '1.0.0'
       or p_canonical_payload #>> '{external_submission_id}' is distinct from p_external_submission_id
       or p_canonical_payload #>> '{event_type}' is distinct from 'PRECHECKOUT_FORM_SUBMITTED'
       or p_canonical_payload #>> '{contract_version}' is distinct from '1.0.0'
       or jsonb_typeof(p_canonical_payload #> '{submitted_at}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{source,tenant_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{source,funnel_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{source,landing_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{identity,email}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{identity,phone_valid}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{lead,full_name}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{commerce,product_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{commerce,offer_ref}') is distinct from 'string'
       or jsonb_typeof(p_canonical_payload #> '{consent,marketing_optin}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{consent,whatsapp_contact}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{assurance,provisional}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{assurance,provider_observed}') is distinct from 'boolean'
       or jsonb_typeof(p_canonical_payload #> '{assurance,activation_authorized}') is distinct from 'boolean'
       or p_canonical_payload #>> '{consent,marketing_optin}' is distinct from 'false'
       or p_canonical_payload #>> '{consent,whatsapp_contact}' is distinct from 'false'
       or p_canonical_payload #>> '{assurance,provisional}' is distinct from 'false'
       or p_canonical_payload #>> '{assurance,provider_observed}' is distinct from 'true'
       or p_canonical_payload #>> '{assurance,activation_authorized}' is distinct from 'false'
       or p_canonical_payload #>> '{source,tenant_ref}' is distinct from 'lancemos'
       or p_canonical_payload #>> '{source,funnel_ref}' is distinct from 'psicologajohanna'
       or p_canonical_payload #>> '{source,landing_ref}' is distinct from 'ads-a'
       or lower(p_canonical_payload #>> '{commerce,product_ref}') is distinct from 'f106691755g'
       or p_canonical_payload #>> '{commerce,offer_ref}' is distinct from 'bxjge6zq'
       or p_canonical_payload #>> '{commerce,price}' is distinct from '49'
       or lower(p_canonical_payload #>> '{commerce,currency}') is distinct from 'usd' then
        raise exception using errcode = '22023', message = 'observed_precheckout_assurance_mismatch';
    end if;

    v_email := nullif(lower(btrim(p_canonical_payload #>> '{identity,email}')), '');
    v_phone := nullif(p_canonical_payload #>> '{identity,phone}', '');
    begin
        v_submitted_at := (p_canonical_payload #>> '{submitted_at}')::timestamptz;
    exception when others then
        raise exception using errcode = '22023', message = 'observed_precheckout_invalid_submitted_at';
    end;

    if v_email is null
       or v_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
       or (v_phone is not null and v_phone !~ '^[1-9][0-9]{7,14}$')
       or v_submitted_at is null
       or nullif(btrim(p_canonical_payload #>> '{lead,full_name}'), '') is null then
        raise exception using errcode = '22023', message = 'observed_precheckout_invalid_canonical_payload';
    end if;

    if (p_canonical_payload #>> '{identity,phone_valid}')::boolean
       is distinct from (v_phone is not null) then
        raise exception using errcode = '22023', message = 'observed_precheckout_phone_assurance_mismatch';
    end if;

    v_content_fingerprint := encode(sha256(convert_to(
        p_raw_payload::text || chr(31) || p_canonical_payload::text,
        'UTF8'
    )), 'hex');

    perform pg_advisory_xact_lock(hashtextextended(p_external_submission_id, 0));
    perform pg_advisory_xact_lock(hashtextextended(concat_ws(
        E'\x1f',
        'lancemos',
        'psicologajohanna',
        'F106691755G',
        'bxjge6zq',
        'email',
        v_email
    ), 0));
    if v_phone is not null then
        perform pg_advisory_xact_lock(hashtextextended(concat_ws(
            E'\x1f',
            'lancemos',
            'psicologajohanna',
            'F106691755G',
            'bxjge6zq',
            'phone',
            v_phone
        ), 0));
    end if;

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
            raise exception using errcode = '55000', message = 'observed_precheckout_without_intent';
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
                    message = 'observed_precheckout_conflict_fingerprint_collision';
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
        '1.0.0',
        p_raw_payload,
        p_canonical_payload,
        false,
        true,
        false
    ) returning id into v_submission_id;

    select pi.id into v_email_intent_id
    from public.purchase_intents pi
    where pi.tenant_ref = 'lancemos'
      and pi.funnel_ref = 'psicologajohanna'
      and pi.product_ref = 'F106691755G'
      and pi.offer_ref = 'bxjge6zq'
      and pi.normalized_email = v_email
      and pi.lifecycle_state = 'waiting_for_purchase'
      and pi.provider_observed;

    if v_phone is not null then
        select pi.id into v_phone_intent_id
        from public.purchase_intents pi
        where pi.tenant_ref = 'lancemos'
          and pi.funnel_ref = 'psicologajohanna'
          and pi.product_ref = 'F106691755G'
          and pi.offer_ref = 'bxjge6zq'
          and pi.normalized_phone = v_phone
          and pi.lifecycle_state = 'waiting_for_purchase'
          and pi.provider_observed;
    end if;

    perform pi.id
    from public.purchase_intents pi
    where pi.id in (v_email_intent_id, v_phone_intent_id)
    order by pi.id
    for update;

    select pi.id into v_email_intent_id
    from public.purchase_intents pi
    where pi.tenant_ref = 'lancemos'
      and pi.funnel_ref = 'psicologajohanna'
      and pi.product_ref = 'F106691755G'
      and pi.offer_ref = 'bxjge6zq'
      and pi.normalized_email = v_email
      and pi.lifecycle_state = 'waiting_for_purchase'
      and pi.provider_observed;

    if v_phone is not null then
        select pi.id into v_phone_intent_id
        from public.purchase_intents pi
        where pi.tenant_ref = 'lancemos'
          and pi.funnel_ref = 'psicologajohanna'
          and pi.product_ref = 'F106691755G'
          and pi.offer_ref = 'bxjge6zq'
          and pi.normalized_phone = v_phone
          and pi.lifecycle_state = 'waiting_for_purchase'
          and pi.provider_observed;
    end if;

    if v_email_intent_id is not null
       and v_phone_intent_id is not null
       and v_email_intent_id is distinct from v_phone_intent_id then
        update public.purchase_intents
        set whatsapp_contact_authorized = false,
            current_classification = 'identity_conflict',
            updated_at = now()
        where id in (v_email_intent_id, v_phone_intent_id);
        v_purchase_intent_id := v_email_intent_id;
    else
        v_purchase_intent_id := coalesce(v_email_intent_id, v_phone_intent_id);
    end if;

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
            'lancemos',
            'psicologajohanna',
            'ads-a',
            'F106691755G',
            'bxjge6zq',
            v_email,
            v_phone,
            v_submitted_at,
            'waiting_for_purchase',
            case when v_phone is null then 'tracking_incomplete' else null end,
            false,
            false,
            true,
            false
        ) returning id into v_purchase_intent_id;
    else
        update public.purchase_intents
        set whatsapp_contact_authorized = false,
            normalized_phone = case
                when normalized_phone is null
                 and v_phone is not null
                 and v_phone_intent_id is null
                    then v_phone
                else normalized_phone
            end,
            current_classification = case
                when current_classification = 'identity_conflict'
                    then 'identity_conflict'
                when normalized_email is distinct from v_email
                  or (
                    normalized_phone is not null
                    and v_phone is not null
                    and normalized_phone is distinct from v_phone
                  )
                    then 'identity_conflict'
                when normalized_phone is null and v_phone is null
                    then 'tracking_incomplete'
                when normalized_phone is null
                 and v_phone is not null
                 and v_phone_intent_id is null
                    then case
                        when current_classification = 'tracking_incomplete' then null
                        else current_classification
                    end
                else current_classification
            end,
            updated_at = now()
        where id = v_purchase_intent_id;
    end if;

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

revoke all on function public.admit_observed_lead_precheckout(text, jsonb, jsonb) from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.admit_observed_lead_precheckout(text, jsonb, jsonb) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.admit_observed_lead_precheckout(text, jsonb, jsonb) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.admit_observed_lead_precheckout(text, jsonb, jsonb)
        to service_role;
    end if;
end;
$acl$;

commit;
