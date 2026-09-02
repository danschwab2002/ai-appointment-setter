-- Portable Hotmart PURCHASE_APPROVED admission, correlation, and stop only.
-- No policy is seeded. No abandonment scheduling, worker, command, message,
-- delivery, scheduled action, recovery case, or outbound effect is created.
-- The legacy Hotmart RPCs and behavior remain unchanged.

begin;

create table public.commercial_ally_hotmart_purchase_policies (
    tenant_ref text not null,
    funnel_ref text not null,
    binding_version integer not null,
    enabled boolean not null default false,
    max_lookback interval not null,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    primary key (tenant_ref, funnel_ref, binding_version),
    foreign key (tenant_ref, funnel_ref, binding_version)
        references public.commercial_ally_runtime_bindings (
            tenant_ref, funnel_ref, binding_version
        ) on delete restrict,
    check (max_lookback > interval '0 seconds'),
    check (max_lookback <= interval '30 days')
);

create table public.portable_hotmart_purchase_correlations (
    webhook_event_id uuid primary key
        references public.webhook_events(id) on delete restrict,
    tenant_ref text not null,
    funnel_ref text not null,
    binding_version integer not null,
    policy_max_lookback interval not null,
    outcome text not null,
    purchase_intent_id uuid references public.purchase_intents(id)
        on delete restrict,
    matched_by text,
    candidate_count integer not null,
    reason_code text not null,
    observed_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp(),
    foreign key (tenant_ref, funnel_ref, binding_version)
        references public.commercial_ally_runtime_bindings (
            tenant_ref, funnel_ref, binding_version
        ) on delete restrict,
    check (policy_max_lookback > interval '0 seconds'),
    check (outcome in ('resolved', 'unmatched', 'ambiguous', 'conflict')),
    check (matched_by is null or matched_by in ('email', 'phone', 'email_and_phone')),
    check (candidate_count >= 0),
    check (nullif(btrim(reason_code), '') is not null),
    check (
        (outcome = 'resolved' and purchase_intent_id is not null
            and matched_by is not null and candidate_count = 1)
        or (outcome <> 'resolved' and purchase_intent_id is null
            and matched_by is null)
    ),
    check (
        (outcome = 'unmatched' and candidate_count = 0)
        or (outcome = 'ambiguous' and candidate_count > 1)
        or (outcome = 'conflict' and candidate_count > 0)
        or outcome = 'resolved'
    )
);

create table public.portable_hotmart_purchase_correlation_candidates (
    webhook_event_id uuid not null
        references public.portable_hotmart_purchase_correlations(webhook_event_id)
        on delete restrict,
    purchase_intent_id uuid not null
        references public.purchase_intents(id) on delete restrict,
    email_match boolean not null,
    phone_match boolean not null,
    created_at timestamptz not null default clock_timestamp(),
    primary key (webhook_event_id, purchase_intent_id),
    check (email_match or phone_match)
);

create trigger portable_hotmart_purchase_correlations_immutable
before update or delete on public.portable_hotmart_purchase_correlations
for each row execute function public.protect_hotmart_purchase_intent_correlation();

create trigger portable_hotmart_purchase_candidates_immutable
before update or delete on public.portable_hotmart_purchase_correlation_candidates
for each row execute function public.protect_hotmart_purchase_intent_correlation();

create function public.admit_portable_hotmart_purchase_approved(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
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
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_policy public.commercial_ally_hotmart_purchase_policies%rowtype;
    v_existing public.portable_hotmart_purchase_correlations%rowtype;
    v_admission_outcome text;
    v_event_id uuid;
    v_email text;
    v_phone text;
    v_approved_at timestamptz;
    v_email_ids uuid[] := array[]::uuid[];
    v_phone_ids uuid[] := array[]::uuid[];
    v_candidate_ids uuid[] := array[]::uuid[];
    v_resolved_intent_id uuid;
    v_correlation_outcome text;
    v_matched_by text;
    v_reason_code text;
    v_candidate_count integer := 0;
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_binding_version is null or p_binding_version < 1
       or p_external_event_id is null or nullif(btrim(p_external_event_id), '') is null
       or p_payload is null or jsonb_typeof(p_payload) <> 'object' then
        raise exception using errcode = '22023', message = 'invalid_portable_hotmart_purchase_input';
    end if;

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = p_tenant_ref
      and binding.funnel_ref = p_funnel_ref
      and binding.binding_version = p_binding_version
      and binding.status = 'active'
    for update;
    if not found then
        raise exception using errcode = '22023', message = 'commercial_ally_binding_unavailable';
    end if;

    if p_payload #>> '{id}' is distinct from p_external_event_id
       or p_payload #>> '{event}' is distinct from 'PURCHASE_APPROVED'
       or p_payload #>> '{version}' is distinct from '2.0.0'
       or p_payload #>> '{data,purchase,status}' is distinct from 'APPROVED'
       or jsonb_typeof(p_payload #> '{data,product,id}') is distinct from 'number'
       or (p_payload #>> '{data,product,id}')::numeric
            is distinct from v_binding.hotmart_product_id::numeric
       or p_payload #>> '{data,purchase,offer,code}'
            is distinct from v_binding.offer_code then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_binding_mismatch';
    end if;

    select policy.* into v_policy
    from public.commercial_ally_hotmart_purchase_policies policy
    where policy.tenant_ref = v_binding.tenant_ref
      and policy.funnel_ref = v_binding.funnel_ref
      and policy.binding_version = v_binding.binding_version
      and policy.enabled
    for update;
    if not found then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_policy_unavailable';
    end if;

    begin
        v_approved_at := to_timestamp(
            (p_payload #>> '{data,purchase,approved_date}')::numeric / 1000
        );
    exception when others then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_invalid_approved_date';
    end;
    if v_approved_at is null then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_invalid_approved_date';
    end if;

    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_event_id
    from public._admit_hotmart_purchase_approved_base(
        p_external_event_id, p_payload
    ) admission;

    if v_admission_outcome = 'semantic_conflict' then
        return query select v_admission_outcome, v_event_id;
        return;
    end if;

    select correlation.* into v_existing
    from public.portable_hotmart_purchase_correlations correlation
    where correlation.webhook_event_id = v_event_id;
    if found then
        if v_existing.tenant_ref is distinct from v_binding.tenant_ref
           or v_existing.funnel_ref is distinct from v_binding.funnel_ref
           or v_existing.binding_version is distinct from v_binding.binding_version then
            raise exception using errcode = '23514', message = 'portable_hotmart_purchase_replay_binding_conflict';
        end if;
        return query select v_admission_outcome, v_event_id;
        return;
    end if;
    if v_admission_outcome = 'duplicate' then
        raise exception using errcode = '23514', message = 'portable_hotmart_purchase_preexisting_admission';
    end if;

    perform public._admit_hotmart_purchase_intent_identity(
        v_event_id, p_normalized_email, p_normalized_phone
    );
    select identity.normalized_email, identity.normalized_phone
    into strict v_email, v_phone
    from public.hotmart_purchase_intent_event_identities identity
    where identity.webhook_event_id = v_event_id;

    perform intent.id
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.offer_ref = v_binding.offer_code
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed
      and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and ((v_email is not null and intent.normalized_email = v_email)
        or (v_phone is not null and intent.normalized_phone = v_phone))
    order by intent.id
    for update;

    select coalesce(array_agg(intent.id order by intent.id), array[]::uuid[])
    into v_email_ids
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.offer_ref = v_binding.offer_code
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and v_email is not null and intent.normalized_email = v_email;

    select coalesce(array_agg(intent.id order by intent.id), array[]::uuid[])
    into v_phone_ids
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.offer_ref = v_binding.offer_code
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and v_phone is not null and intent.normalized_phone = v_phone;

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
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_email_ids) = 1 and cardinality(v_phone_ids) = 1
          and v_email_ids[1] = v_phone_ids[1] then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_email_ids[1];
            v_matched_by := 'email_and_phone';
            v_reason_code := 'exact_email_and_phone';
            v_candidate_count := 1;
        elsif cardinality(v_email_ids) = 0 or cardinality(v_phone_ids) = 0
          or not exists (
              select 1 from unnest(v_email_ids) email_id
              join unnest(v_phone_ids) phone_id on phone_id = email_id
          ) then
            v_correlation_outcome := 'conflict';
            v_reason_code := 'email_phone_conflict';
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    elsif v_email is not null then
        if cardinality(v_email_ids) = 0 then
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_email_ids) = 1 then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_email_ids[1];
            v_matched_by := 'email';
            v_reason_code := 'exact_email';
            v_candidate_count := 1;
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    else
        if cardinality(v_phone_ids) = 0 then
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_phone_ids) = 1 then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_phone_ids[1];
            v_matched_by := 'phone';
            v_reason_code := 'exact_phone';
            v_candidate_count := 1;
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    end if;

    insert into public.portable_hotmart_purchase_correlations (
        webhook_event_id, tenant_ref, funnel_ref, binding_version,
        policy_max_lookback, outcome, purchase_intent_id, matched_by,
        candidate_count, reason_code, observed_at
    ) values (
        v_event_id, v_binding.tenant_ref, v_binding.funnel_ref,
        v_binding.binding_version, v_policy.max_lookback,
        v_correlation_outcome, v_resolved_intent_id, v_matched_by,
        v_candidate_count, v_reason_code, v_approved_at
    );

    if cardinality(v_candidate_ids) > 0 then
        insert into public.portable_hotmart_purchase_correlation_candidates (
            webhook_event_id, purchase_intent_id, email_match, phone_match
        )
        select v_event_id, candidate_id,
               candidate_id = any(v_email_ids), candidate_id = any(v_phone_ids)
        from unnest(v_candidate_ids) candidate_id;
    end if;

    if v_correlation_outcome = 'resolved' then
        update public.purchase_intents
        set lifecycle_state = 'purchased',
            current_classification = null,
            activation_authorized = false,
            updated_at = clock_timestamp()
        where id = v_resolved_intent_id
          and lifecycle_state = 'waiting_for_purchase';
        if not found then
            raise exception using errcode = '40001', message = 'purchase_intent_changed_concurrently';
        end if;
        perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(
            v_resolved_intent_id, clock_timestamp()
        );
    end if;

    return query select v_admission_outcome, v_event_id;
end;
$function$;

alter table public.commercial_ally_hotmart_purchase_policies enable row level security;
alter table public.portable_hotmart_purchase_correlations enable row level security;
alter table public.portable_hotmart_purchase_correlation_candidates enable row level security;

revoke all on table public.commercial_ally_hotmart_purchase_policies from public;
revoke all on table public.portable_hotmart_purchase_correlations from public;
revoke all on table public.portable_hotmart_purchase_correlation_candidates from public;
revoke all on function public.admit_portable_hotmart_purchase_approved(
    text, text, integer, text, jsonb, text, text
) from public;

do $acl$
declare
    v_role text;
begin
    for v_role in
        select role.rolname from pg_roles role
        where role.rolname in ('anon', 'authenticated', 'service_role')
    loop
        execute format(
            'revoke all on table public.commercial_ally_hotmart_purchase_policies from %I',
            v_role
        );
        execute format(
            'revoke all on table public.portable_hotmart_purchase_correlations from %I',
            v_role
        );
        execute format(
            'revoke all on table public.portable_hotmart_purchase_correlation_candidates from %I',
            v_role
        );
        execute format(
            'revoke all on function public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text) from %I',
            v_role
        );
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.admit_portable_hotmart_purchase_approved(
            text, text, integer, text, jsonb, text, text
        ) to service_role;
    end if;
end
$acl$;

commit;
