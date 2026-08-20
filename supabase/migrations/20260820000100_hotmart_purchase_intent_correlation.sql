-- Durable, deterministic correlation between observed pre-checkout intents and
-- authoritative Hotmart PURCHASE_APPROVED / PURCHASE_OUT_OF_SHOPPING_CART events.
-- This migration creates no recovery case, scheduled action, sequence, command,
-- authorization, agent invocation, or outbound effect.

begin;

-- Expand phase: preserve the historical RPC names as safe shims below while
-- moving their original admission implementations behind owner-only names.
alter function public.admit_hotmart_purchase_approved(text, jsonb)
    rename to _admit_hotmart_purchase_approved_base;
alter function public.admit_hotmart_cart_abandonment(text, jsonb)
    rename to _admit_hotmart_cart_abandonment_base;

create table public.hotmart_purchase_intent_scopes (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null,
    funnel_ref text not null,
    hotmart_product_id text not null,
    purchase_intent_product_ref text not null,
    offer_ref text not null,
    max_lookback interval not null,
    active boolean not null default false,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    check (nullif(btrim(tenant_ref), '') is not null),
    check (nullif(btrim(funnel_ref), '') is not null),
    check (hotmart_product_id ~ '^[1-9][0-9]*$'),
    check (nullif(btrim(purchase_intent_product_ref), '') is not null),
    check (nullif(btrim(offer_ref), '') is not null),
    check (max_lookback > interval '0 seconds'),
    check (max_lookback <= interval '30 days')
);

create unique index hotmart_purchase_intent_scopes_one_active_idx
on public.hotmart_purchase_intent_scopes (hotmart_product_id, offer_ref)
where active;

create table public.hotmart_purchase_intent_event_identities (
    webhook_event_id uuid primary key
        references public.webhook_events(id) on delete restrict,
    normalized_email text,
    normalized_phone text,
    created_at timestamptz not null default clock_timestamp(),
    check (normalized_email is not null or normalized_phone is not null),
    check (
        normalized_email is null
        or normalized_email = lower(btrim(normalized_email))
    ),
    check (
        normalized_phone is null
        or normalized_phone ~ '^[1-9][0-9]{7,14}$'
    )
);

create table public.hotmart_purchase_intent_correlations (
    webhook_event_id uuid primary key
        references public.webhook_events(id) on delete restrict,
    scope_id uuid references public.hotmart_purchase_intent_scopes(id)
        on delete restrict,
    event_type text not null,
    outcome text not null,
    purchase_intent_id uuid references public.purchase_intents(id)
        on delete restrict,
    matched_by text,
    candidate_count integer not null,
    reason_code text not null,
    manual_handoff_required boolean not null,
    observed_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp(),
    check (event_type in (
        'PURCHASE_APPROVED', 'PURCHASE_OUT_OF_SHOPPING_CART'
    )),
    check (outcome in ('resolved', 'unmatched', 'ambiguous', 'conflict')),
    check (matched_by is null or matched_by in (
        'email', 'phone', 'email_and_phone'
    )),
    check (candidate_count >= 0),
    check (nullif(btrim(reason_code), '') is not null),
    check (
        (
            outcome = 'resolved'
            and purchase_intent_id is not null
            and matched_by is not null
            and candidate_count = 1
            and not manual_handoff_required
        )
        or (
            outcome <> 'resolved'
            and purchase_intent_id is null
            and matched_by is null
            and manual_handoff_required
        )
    ),
    check (
        (outcome = 'unmatched' and candidate_count = 0)
        or (outcome = 'ambiguous' and candidate_count > 1)
        or (outcome = 'conflict' and candidate_count > 0)
        or outcome = 'resolved'
    )
);

create table public.hotmart_purchase_intent_correlation_candidates (
    webhook_event_id uuid not null
        references public.hotmart_purchase_intent_correlations(webhook_event_id)
        on delete restrict,
    purchase_intent_id uuid not null
        references public.purchase_intents(id) on delete restrict,
    email_match boolean not null,
    phone_match boolean not null,
    created_at timestamptz not null default clock_timestamp(),
    primary key (webhook_event_id, purchase_intent_id),
    check (email_match or phone_match)
);

alter table public.hotmart_purchase_intent_scopes enable row level security;
alter table public.hotmart_purchase_intent_event_identities enable row level security;
alter table public.hotmart_purchase_intent_correlations enable row level security;
alter table public.hotmart_purchase_intent_correlation_candidates enable row level security;

insert into public.hotmart_purchase_intent_scopes (
    tenant_ref,
    funnel_ref,
    hotmart_product_id,
    purchase_intent_product_ref,
    offer_ref,
    max_lookback,
    active
) values (
    'lancemos',
    'psicologajohanna',
    '8104005',
    'f106691755g',
    'bxjge6zq',
    interval '24 hours',
    true
);

create or replace function public.hotmart_purchase_intent_payload_is_processable(
    p_webhook_event_id uuid
)
returns boolean
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_event public.webhook_events%rowtype;
begin
    select event.* into v_event
    from public.webhook_events event
    where event.id = p_webhook_event_id;

    if not found or v_event.source <> 'hotmart' then
        return false;
    end if;
    if v_event.event_type = 'PURCHASE_APPROVED' then
        return coalesce(public.hotmart_purchase_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ), false);
    end if;
    if v_event.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART' then
        return coalesce(public.hotmart_cart_abandonment_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ), false);
    end if;
    return false;
end;
$function$;

create or replace function public.correlate_hotmart_purchase_intent(
    p_webhook_event_id uuid
)
returns table (
    outcome text,
    purchase_intent_id uuid,
    matched_by text,
    candidate_count integer,
    manual_handoff_required boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_event public.webhook_events%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_existing public.hotmart_purchase_intent_correlations%rowtype;
    v_event_type text;
    v_email text;
    v_phone text;
    v_product_id text;
    v_offer_ref text;
    v_observed_at timestamptz;
    v_email_ids uuid[] := array[]::uuid[];
    v_phone_ids uuid[] := array[]::uuid[];
    v_candidate_ids uuid[] := array[]::uuid[];
    v_resolved_intent_id uuid;
    v_outcome text;
    v_matched_by text;
    v_reason_code text;
    v_candidate_count integer := 0;
    v_manual_handoff boolean := true;
begin
    if p_webhook_event_id is null then
        raise exception using errcode = '22023', message = 'invalid_hotmart_intent_correlation_input';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_webhook_event_id::text, 0));

    select correlation.* into v_existing
    from public.hotmart_purchase_intent_correlations correlation
    where correlation.webhook_event_id = p_webhook_event_id;

    if found then
        return query select
            v_existing.outcome,
            v_existing.purchase_intent_id,
            v_existing.matched_by,
            v_existing.candidate_count,
            v_existing.manual_handoff_required;
        return;
    end if;

    select event.* into v_event
    from public.webhook_events event
    where event.id = p_webhook_event_id
    for update;

    if not found then
        raise exception using errcode = 'P0002', message = 'hotmart_webhook_event_not_found';
    end if;
    if not public.hotmart_purchase_intent_payload_is_processable(p_webhook_event_id) then
        raise exception using errcode = '22023', message = 'hotmart_intent_event_not_processable';
    end if;

    select identity.normalized_email, identity.normalized_phone
    into v_email, v_phone
    from public.hotmart_purchase_intent_event_identities identity
    where identity.webhook_event_id = p_webhook_event_id;

    if not found then
        raise exception using
            errcode = 'P0002',
            message = 'hotmart_intent_identity_not_admitted';
    end if;

    v_event_type := v_event.event_type;
    v_product_id := v_event.payload #>> '{data,product,id}';

    if v_event_type = 'PURCHASE_APPROVED' then
        v_offer_ref := nullif(btrim(
            v_event.payload #>> '{data,purchase,offer,code}'
        ), '');
        v_observed_at := to_timestamp(
            (v_event.payload #>> '{data,purchase,approved_date}')::numeric / 1000
        );
    else
        v_offer_ref := nullif(btrim(v_event.payload #>> '{data,offer,code}'), '');
        v_observed_at := to_timestamp(
            (v_event.payload ->> 'creation_date')::numeric / 1000
        );
    end if;

    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.active
      and scope.hotmart_product_id = v_product_id
      and scope.offer_ref = v_offer_ref
    for share;

    if not found then
        v_outcome := 'unmatched';
        v_reason_code := 'scope_not_configured';
    else
        perform 1
        from public.purchase_intents intent
        where intent.tenant_ref = v_scope.tenant_ref
          and intent.funnel_ref = v_scope.funnel_ref
          and lower(intent.product_ref) = lower(v_scope.purchase_intent_product_ref)
          and intent.offer_ref = v_scope.offer_ref
          and intent.lifecycle_state = 'waiting_for_purchase'
          and intent.provider_observed
          and not intent.provisional
          and intent.submitted_at >= v_observed_at - v_scope.max_lookback
          and intent.submitted_at <= v_observed_at
          and (
              (v_email is not null and intent.normalized_email = v_email)
              or (v_phone is not null and intent.normalized_phone = v_phone)
          )
        order by intent.id
        for update;

        select coalesce(array_agg(intent.id order by intent.id), array[]::uuid[])
        into v_email_ids
        from public.purchase_intents intent
        where intent.tenant_ref = v_scope.tenant_ref
          and intent.funnel_ref = v_scope.funnel_ref
          and lower(intent.product_ref) = lower(v_scope.purchase_intent_product_ref)
          and intent.offer_ref = v_scope.offer_ref
          and intent.lifecycle_state = 'waiting_for_purchase'
          and intent.provider_observed
          and not intent.provisional
          and intent.submitted_at >= v_observed_at - v_scope.max_lookback
          and intent.submitted_at <= v_observed_at
          and v_email is not null
          and intent.normalized_email = v_email;

        select coalesce(array_agg(intent.id order by intent.id), array[]::uuid[])
        into v_phone_ids
        from public.purchase_intents intent
        where intent.tenant_ref = v_scope.tenant_ref
          and intent.funnel_ref = v_scope.funnel_ref
          and lower(intent.product_ref) = lower(v_scope.purchase_intent_product_ref)
          and intent.offer_ref = v_scope.offer_ref
          and intent.lifecycle_state = 'waiting_for_purchase'
          and intent.provider_observed
          and not intent.provisional
          and intent.submitted_at >= v_observed_at - v_scope.max_lookback
          and intent.submitted_at <= v_observed_at
          and v_phone is not null
          and intent.normalized_phone = v_phone;

        select coalesce(array_agg(candidate_id order by candidate_id), array[]::uuid[])
        into v_candidate_ids
        from (
            select unnest(v_email_ids) as candidate_id
            union
            select unnest(v_phone_ids) as candidate_id
        ) candidates;
        v_candidate_count := cardinality(v_candidate_ids);

        if v_email is not null and v_phone is not null then
            if cardinality(v_email_ids) = 0 and cardinality(v_phone_ids) = 0 then
                v_outcome := 'unmatched';
                v_reason_code := 'identity_not_found';
            elsif cardinality(v_email_ids) = 1
               and cardinality(v_phone_ids) = 1
               and v_email_ids[1] = v_phone_ids[1] then
                v_outcome := 'resolved';
                v_resolved_intent_id := v_email_ids[1];
                v_matched_by := 'email_and_phone';
                v_reason_code := 'exact_email_and_phone';
                v_candidate_count := 1;
                v_manual_handoff := false;
            elsif cardinality(v_email_ids) = 0
               or cardinality(v_phone_ids) = 0
               or not exists (
                   select 1
                   from unnest(v_email_ids) email_id
                   join unnest(v_phone_ids) phone_id on phone_id = email_id
               ) then
                v_outcome := 'conflict';
                v_reason_code := 'email_phone_conflict';
            else
                v_outcome := 'ambiguous';
                v_reason_code := 'multiple_candidates';
            end if;
        elsif v_email is not null then
            if cardinality(v_email_ids) = 0 then
                v_outcome := 'unmatched';
                v_reason_code := 'identity_not_found';
            elsif cardinality(v_email_ids) = 1 then
                v_outcome := 'resolved';
                v_resolved_intent_id := v_email_ids[1];
                v_matched_by := 'email';
                v_reason_code := 'exact_email';
                v_candidate_count := 1;
                v_manual_handoff := false;
            else
                v_outcome := 'ambiguous';
                v_reason_code := 'multiple_candidates';
            end if;
        else
            if cardinality(v_phone_ids) = 0 then
                v_outcome := 'unmatched';
                v_reason_code := 'identity_not_found';
            elsif cardinality(v_phone_ids) = 1 then
                v_outcome := 'resolved';
                v_resolved_intent_id := v_phone_ids[1];
                v_matched_by := 'phone';
                v_reason_code := 'exact_phone';
                v_candidate_count := 1;
                v_manual_handoff := false;
            else
                v_outcome := 'ambiguous';
                v_reason_code := 'multiple_candidates';
            end if;
        end if;
    end if;

    insert into public.hotmart_purchase_intent_correlations (
        webhook_event_id,
        scope_id,
        event_type,
        outcome,
        purchase_intent_id,
        matched_by,
        candidate_count,
        reason_code,
        manual_handoff_required,
        observed_at
    ) values (
        p_webhook_event_id,
        v_scope.id,
        v_event_type,
        v_outcome,
        v_resolved_intent_id,
        v_matched_by,
        v_candidate_count,
        v_reason_code,
        v_manual_handoff,
        v_observed_at
    );

    if v_scope.id is not null and cardinality(v_candidate_ids) > 0 then
        insert into public.hotmart_purchase_intent_correlation_candidates (
            webhook_event_id,
            purchase_intent_id,
            email_match,
            phone_match
        )
        select
            p_webhook_event_id,
            candidate_id,
            candidate_id = any(v_email_ids),
            candidate_id = any(v_phone_ids)
        from unnest(v_candidate_ids) candidate_id;
    end if;

    if v_outcome = 'resolved' then
        if v_event_type = 'PURCHASE_APPROVED' then
            update public.purchase_intents
            set lifecycle_state = 'purchased',
                current_classification = null,
                activation_authorized = false,
                updated_at = clock_timestamp()
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
        else
            update public.purchase_intents
            set current_classification = 'abandonment_candidate',
                activation_authorized = false,
                updated_at = clock_timestamp()
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
        end if;
        if not found then
            raise exception using errcode = '40001', message = 'purchase_intent_changed_concurrently';
        end if;
    elsif v_outcome = 'conflict' then
        update public.purchase_intents
        set current_classification = 'identity_conflict',
            activation_authorized = false,
            updated_at = clock_timestamp()
        where id = any(v_candidate_ids)
          and lifecycle_state = 'waiting_for_purchase';
    elsif v_outcome = 'ambiguous' then
        update public.purchase_intents
        set current_classification = 'tracking_incomplete',
            activation_authorized = false,
            updated_at = clock_timestamp()
        where id = any(v_candidate_ids)
          and lifecycle_state = 'waiting_for_purchase';
    end if;

    return query select
        v_outcome,
        v_resolved_intent_id,
        v_matched_by,
        v_candidate_count,
        v_manual_handoff;
end;
$function$;

create or replace function public._normalize_hotmart_purchase_intent_phone(
    p_value text
)
returns text
language plpgsql
immutable
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_digits text;
begin
    if p_value is null or p_value !~ '^\+?[0-9 ()-]+$' then
        return null;
    end if;
    v_digits := regexp_replace(p_value, '[^0-9]', '', 'g');
    if v_digits !~ '^[1-9][0-9]{7,14}$' then
        return null;
    end if;
    return v_digits;
end;
$function$;

create or replace function public._hotmart_purchase_intent_payload_identity(
    p_event_type text,
    p_payload jsonb
)
returns table (
    normalized_email text,
    normalized_phone text
)
language plpgsql
immutable
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_email_value jsonb;
    v_phone_value jsonb;
    v_checkout_phone_value jsonb;
begin
    v_email_value := p_payload #> '{data,buyer,email}';
    normalized_email := case
        when jsonb_typeof(v_email_value) = 'string'
            then nullif(lower(btrim(p_payload #>> '{data,buyer,email}')), '')
        else null
    end;

    if p_event_type = 'PURCHASE_APPROVED' then
        v_checkout_phone_value := p_payload #> '{data,buyer,checkout_phone}';
        normalized_phone := case
            when jsonb_typeof(v_checkout_phone_value) = 'string'
                then public._normalize_hotmart_purchase_intent_phone(
                    p_payload #>> '{data,buyer,checkout_phone}'
                )
            else null
        end;
    elsif p_event_type = 'PURCHASE_OUT_OF_SHOPPING_CART' then
        v_phone_value := p_payload #> '{data,buyer,phone}';
        v_checkout_phone_value := p_payload #> '{data,buyer,checkout_phone}';
        normalized_phone := coalesce(
            case
                when jsonb_typeof(v_phone_value) = 'string'
                    then public._normalize_hotmart_purchase_intent_phone(
                        p_payload #>> '{data,buyer,phone}'
                    )
                else null
            end,
            case
                when jsonb_typeof(v_checkout_phone_value) = 'string'
                    then public._normalize_hotmart_purchase_intent_phone(
                        p_payload #>> '{data,buyer,checkout_phone}'
                    )
                else null
            end
        );
    else
        raise exception using
            errcode = '22023',
            message = 'unsupported_hotmart_intent_event';
    end if;

    return next;
end;
$function$;

create or replace function public._admit_hotmart_purchase_intent_identity(
    p_webhook_event_id uuid,
    p_normalized_email text,
    p_normalized_phone text
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_email text;
    v_phone text;
    v_event_type text;
    v_payload jsonb;
    v_payload_email text;
    v_payload_phone text;
    v_existing public.hotmart_purchase_intent_event_identities%rowtype;
begin
    v_email := nullif(lower(btrim(p_normalized_email)), '');
    v_phone := public._normalize_hotmart_purchase_intent_phone(p_normalized_phone);

    select event.event_type, event.payload
    into strict v_event_type, v_payload
    from public.webhook_events event
    where event.id = p_webhook_event_id
      and event.source = 'hotmart'
    for update;

    select identity.normalized_email, identity.normalized_phone
    into strict v_payload_email, v_payload_phone
    from public._hotmart_purchase_intent_payload_identity(
        v_event_type, v_payload
    ) identity;

    if v_email is distinct from v_payload_email
       or v_phone is distinct from v_payload_phone then
        raise exception using
            errcode = '23514',
            message = 'hotmart_intent_identity_payload_mismatch';
    end if;
    if v_email is null and v_phone is null then
        raise exception using
            errcode = '22023',
            message = 'missing_hotmart_normalized_identity';
    end if;

    insert into public.hotmart_purchase_intent_event_identities (
        webhook_event_id, normalized_email, normalized_phone
    ) values (
        p_webhook_event_id, v_email, v_phone
    ) on conflict (webhook_event_id) do nothing;

    select identity.* into strict v_existing
    from public.hotmart_purchase_intent_event_identities identity
    where identity.webhook_event_id = p_webhook_event_id;

    if v_existing.normalized_email is distinct from v_email
       or v_existing.normalized_phone is distinct from v_phone then
        raise exception using
            errcode = '23514',
            message = 'hotmart_normalized_identity_conflict';
    end if;
end;
$function$;

create or replace function public.admit_and_correlate_hotmart_purchase_approved(
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_outcome text;
    v_event_id uuid;
begin
    select admission.outcome, admission.webhook_event_id
    into strict v_outcome, v_event_id
    from public._admit_hotmart_purchase_approved_base(
        p_external_event_id, p_payload
    ) admission;

    if v_outcome <> 'semantic_conflict' then
        perform public._admit_hotmart_purchase_intent_identity(
            v_event_id, p_normalized_email, p_normalized_phone
        );
        perform * from public.correlate_hotmart_purchase_intent(v_event_id);
    end if;

    return query select v_outcome, v_event_id;
end;
$function$;

create or replace function public.admit_and_correlate_hotmart_cart_abandonment(
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_outcome text;
    v_event_id uuid;
begin
    select admission.outcome, admission.webhook_event_id
    into strict v_outcome, v_event_id
    from public._admit_hotmart_cart_abandonment_base(
        p_external_event_id, p_payload
    ) admission;

    if v_outcome <> 'semantic_conflict' then
        perform public._admit_hotmart_purchase_intent_identity(
            v_event_id, p_normalized_email, p_normalized_phone
        );
        perform * from public.correlate_hotmart_purchase_intent(v_event_id);
    end if;

    return query select v_outcome, v_event_id;
end;
$function$;

create or replace function public.admit_hotmart_purchase_approved(
    p_external_event_id text,
    p_payload jsonb
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_email text;
    v_phone text;
begin
    select identity.normalized_email, identity.normalized_phone
    into strict v_email, v_phone
    from public._hotmart_purchase_intent_payload_identity(
        'PURCHASE_APPROVED', p_payload
    ) identity;

    return query
    select admission.outcome, admission.webhook_event_id
    from public.admit_and_correlate_hotmart_purchase_approved(
        p_external_event_id, p_payload, v_email, v_phone
    ) admission;
end;
$function$;

create or replace function public.admit_hotmart_cart_abandonment(
    p_external_event_id text,
    p_payload jsonb
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_email text;
    v_phone text;
begin
    select identity.normalized_email, identity.normalized_phone
    into strict v_email, v_phone
    from public._hotmart_purchase_intent_payload_identity(
        'PURCHASE_OUT_OF_SHOPPING_CART', p_payload
    ) identity;

    return query
    select admission.outcome, admission.webhook_event_id
    from public.admit_and_correlate_hotmart_cart_abandonment(
        p_external_event_id, p_payload, v_email, v_phone
    ) admission;
end;
$function$;

create or replace function public.protect_hotmart_purchase_intent_correlation()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '23514', message = 'hotmart_purchase_intent_correlation_immutable';
end;
$function$;

create trigger hotmart_purchase_intent_correlations_immutable
before update or delete on public.hotmart_purchase_intent_correlations
for each row execute function public.protect_hotmart_purchase_intent_correlation();

create trigger hotmart_purchase_intent_event_identities_immutable
before update or delete on public.hotmart_purchase_intent_event_identities
for each row execute function public.protect_hotmart_purchase_intent_correlation();

create trigger hotmart_purchase_intent_candidates_immutable
before update or delete on public.hotmart_purchase_intent_correlation_candidates
for each row execute function public.protect_hotmart_purchase_intent_correlation();

revoke all on table public.hotmart_purchase_intent_scopes from public;
revoke all on table public.hotmart_purchase_intent_event_identities from public;
revoke all on table public.hotmart_purchase_intent_correlations from public;
revoke all on table public.hotmart_purchase_intent_correlation_candidates from public;
revoke all on function public.hotmart_purchase_intent_payload_is_processable(uuid) from public;
revoke all on function public._normalize_hotmart_purchase_intent_phone(text) from public;
revoke all on function public._hotmart_purchase_intent_payload_identity(text, jsonb) from public;
revoke all on function public._admit_hotmart_purchase_intent_identity(uuid, text, text) from public;
revoke all on function public._admit_hotmart_purchase_approved_base(text, jsonb) from public;
revoke all on function public._admit_hotmart_cart_abandonment_base(text, jsonb) from public;
revoke all on function public.admit_hotmart_purchase_approved(text, jsonb) from public;
revoke all on function public.admit_hotmart_cart_abandonment(text, jsonb) from public;
revoke all on function public.admit_and_correlate_hotmart_purchase_approved(text, jsonb, text, text) from public;
revoke all on function public.admit_and_correlate_hotmart_cart_abandonment(text, jsonb, text, text) from public;
revoke all on function public.correlate_hotmart_purchase_intent(uuid) from public;
revoke all on function public.protect_hotmart_purchase_intent_correlation() from public;

-- Supabase roles are optional in role-neutral PostgreSQL test environments.
do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.hotmart_purchase_intent_scopes from anon;
        revoke all on table public.hotmart_purchase_intent_event_identities from anon;
        revoke all on table public.hotmart_purchase_intent_correlations from anon;
        revoke all on table public.hotmart_purchase_intent_correlation_candidates from anon;
        revoke all on function public.hotmart_purchase_intent_payload_is_processable(uuid) from anon;
        revoke all on function public._normalize_hotmart_purchase_intent_phone(text) from anon;
        revoke all on function public._hotmart_purchase_intent_payload_identity(text, jsonb) from anon;
        revoke all on function public._admit_hotmart_purchase_intent_identity(uuid, text, text) from anon;
        revoke all on function public._admit_hotmart_purchase_approved_base(text, jsonb) from anon;
        revoke all on function public._admit_hotmart_cart_abandonment_base(text, jsonb) from anon;
        revoke all on function public.admit_hotmart_purchase_approved(text, jsonb) from anon;
        revoke all on function public.admit_hotmart_cart_abandonment(text, jsonb) from anon;
        revoke all on function public.admit_and_correlate_hotmart_purchase_approved(text, jsonb, text, text) from anon;
        revoke all on function public.admit_and_correlate_hotmart_cart_abandonment(text, jsonb, text, text) from anon;
        revoke all on function public.correlate_hotmart_purchase_intent(uuid) from anon;
        revoke all on function public.protect_hotmart_purchase_intent_correlation() from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.hotmart_purchase_intent_scopes from authenticated;
        revoke all on table public.hotmart_purchase_intent_event_identities from authenticated;
        revoke all on table public.hotmart_purchase_intent_correlations from authenticated;
        revoke all on table public.hotmart_purchase_intent_correlation_candidates from authenticated;
        revoke all on function public.hotmart_purchase_intent_payload_is_processable(uuid) from authenticated;
        revoke all on function public._normalize_hotmart_purchase_intent_phone(text) from authenticated;
        revoke all on function public._hotmart_purchase_intent_payload_identity(text, jsonb) from authenticated;
        revoke all on function public._admit_hotmart_purchase_intent_identity(uuid, text, text) from authenticated;
        revoke all on function public._admit_hotmart_purchase_approved_base(text, jsonb) from authenticated;
        revoke all on function public._admit_hotmart_cart_abandonment_base(text, jsonb) from authenticated;
        revoke all on function public.admit_hotmart_purchase_approved(text, jsonb) from authenticated;
        revoke all on function public.admit_hotmart_cart_abandonment(text, jsonb) from authenticated;
        revoke all on function public.admit_and_correlate_hotmart_purchase_approved(text, jsonb, text, text) from authenticated;
        revoke all on function public.admit_and_correlate_hotmart_cart_abandonment(text, jsonb, text, text) from authenticated;
        revoke all on function public.correlate_hotmart_purchase_intent(uuid) from authenticated;
        revoke all on function public.protect_hotmart_purchase_intent_correlation() from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.hotmart_purchase_intent_scopes from service_role;
        revoke all on table public.hotmart_purchase_intent_event_identities from service_role;
        revoke all on table public.hotmart_purchase_intent_correlations from service_role;
        revoke all on table public.hotmart_purchase_intent_correlation_candidates from service_role;
        revoke all on function public.hotmart_purchase_intent_payload_is_processable(uuid) from service_role;
        revoke all on function public._normalize_hotmart_purchase_intent_phone(text) from service_role;
        revoke all on function public._hotmart_purchase_intent_payload_identity(text, jsonb) from service_role;
        revoke all on function public._admit_hotmart_purchase_intent_identity(uuid, text, text) from service_role;
        revoke all on function public._admit_hotmart_purchase_approved_base(text, jsonb) from service_role;
        revoke all on function public._admit_hotmart_cart_abandonment_base(text, jsonb) from service_role;
        revoke all on function public.admit_hotmart_purchase_approved(text, jsonb) from service_role;
        revoke all on function public.admit_hotmart_cart_abandonment(text, jsonb) from service_role;
        revoke all on function public.admit_and_correlate_hotmart_purchase_approved(text, jsonb, text, text) from service_role;
        revoke all on function public.admit_and_correlate_hotmart_cart_abandonment(text, jsonb, text, text) from service_role;
        revoke all on function public.correlate_hotmart_purchase_intent(uuid) from service_role;
        revoke all on function public.protect_hotmart_purchase_intent_correlation() from service_role;
        grant execute on function public.admit_and_correlate_hotmart_purchase_approved(text, jsonb, text, text) to service_role;
        grant execute on function public.admit_and_correlate_hotmart_cart_abandonment(text, jsonb, text, text) to service_role;
        grant execute on function public.correlate_hotmart_purchase_intent(uuid) to service_role;
        grant execute on function public.admit_hotmart_purchase_approved(text, jsonb) to service_role;
        grant execute on function public.admit_hotmart_cart_abandonment(text, jsonb) to service_role;
    end if;
end;
$acl$;

commit;
