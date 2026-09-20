-- Durable AI-assisted pre-resolution before an unresolved correlation reaches Slack.
-- The model receives only bounded, non-PII evidence and may recommend one existing
-- candidate or abstain. A recommendation never resolves a case or authorizes contact.

begin;

alter table public.slack_correlation_notification_projection
    drop constraint slack_correlation_notification_projecti_projection_status_check;

alter table public.slack_correlation_notification_projection
    add constraint slack_correlation_notification_projection_projection_status_check
    check (projection_status in (
        'pending', 'leased', 'retryable_failed', 'admitted',
        'terminal_failed', 'suppressed'
    ));

alter table public.slack_correlation_notification_projection
    drop constraint slack_correlation_notification_contract_version_check;

alter table public.slack_correlation_notification_projection
    add constraint slack_correlation_notification_contract_version_check
    check (notification_contract_version in (1, 2, 3));

create table public.operator_correlation_preresolutions (
    webhook_event_id uuid not null
        references public.hotmart_purchase_intent_correlations(webhook_event_id)
        on delete restrict,
    preresolution_version integer not null default 1,
    tenant_ref text not null,
    funnel_ref text not null,
    source_event_type text not null,
    outcome text not null,
    reason_code text not null,
    candidate_count integer not null,
    status text not null default 'pending',
    recommendation_ref uuid unique,
    recommended_purchase_intent_id uuid
        references public.purchase_intents(id) on delete restrict,
    evidence_snapshot jsonb,
    evidence_fingerprint text,
    supporting_evidence jsonb not null default '[]'::jsonb,
    model_name text,
    prompt_version text,
    attempt_count integer not null default 0,
    next_attempt_at timestamptz,
    lease_owner text,
    claim_token uuid,
    lease_generation bigint not null default 0,
    lease_expires_at timestamptz,
    last_failure_code text,
    decided_at timestamptz,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    primary key (webhook_event_id, preresolution_version),
    check (preresolution_version >= 1),
    check (nullif(btrim(tenant_ref), '') is not null),
    check (nullif(btrim(funnel_ref), '') is not null),
    check (source_event_type in (
        'PURCHASE_APPROVED', 'PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED'
    )),
    check (outcome in ('unmatched', 'ambiguous', 'conflict')),
    check (nullif(btrim(reason_code), '') is not null),
    check (
        (outcome = 'unmatched' and candidate_count = 0)
        or (outcome = 'ambiguous' and candidate_count > 1)
        or (outcome = 'conflict' and candidate_count > 0)
    ),
    check (status in (
        'pending', 'leased', 'retryable_failed', 'recommended',
        'abstained', 'suppressed_unmatched', 'terminal_failed'
    )),
    check (attempt_count >= 0),
    check (lease_generation >= 0),
    check (jsonb_typeof(supporting_evidence) = 'array'),
    check (evidence_snapshot is null or jsonb_typeof(evidence_snapshot) = 'object'),
    check (
        (evidence_snapshot is null and evidence_fingerprint is null)
        or (evidence_snapshot is not null
            and evidence_fingerprint ~ '^[a-f0-9]{64}$')
    ),
    check (
        (status = 'leased'
            and lease_owner is not null
            and claim_token is not null
            and lease_expires_at is not null)
        or (status <> 'leased'
            and lease_owner is null
            and claim_token is null
            and lease_expires_at is null)
    ),
    check (
        (status = 'recommended'
            and recommendation_ref is not null
            and recommended_purchase_intent_id is not null
            and model_name is not null
            and prompt_version is not null
            and jsonb_array_length(supporting_evidence) > 0
            and decided_at is not null)
        or (status <> 'recommended'
            and recommendation_ref is null
            and recommended_purchase_intent_id is null
            and supporting_evidence = '[]'::jsonb)
    ),
    check (
        status not in ('abstained', 'suppressed_unmatched')
        or decided_at is not null
    ),
    check (
        status <> 'suppressed_unmatched' or outcome = 'unmatched'
    )
);

create index operator_correlation_preresolutions_claim_idx
    on public.operator_correlation_preresolutions (
        tenant_ref,
        funnel_ref,
        status,
        next_attempt_at,
        created_at,
        webhook_event_id
    );

alter table public.operator_correlation_preresolutions enable row level security;

create or replace function public.enforce_slack_correlation_preresolution_gate()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if exists (
        select 1
        from public.operator_correlation_preresolutions pre
        where pre.webhook_event_id = new.source_event_id
          and pre.status = 'recommended'
          and pre.evidence_snapshot is not null
          and pre.evidence_fingerprint = encode(
              sha256(convert_to(pre.evidence_snapshot::text, 'UTF8')),
              'hex'
          )
    ) then
        return new;
    end if;

    new.projection_status := 'suppressed';
    new.notification_contract_version := null;
    new.next_attempt_at := null;
    new.lease_owner := null;
    new.claim_token := null;
    new.lease_expires_at := null;
    new.notification_id := null;
    new.last_failure_code := null;
    return new;
end;
$function$;

create trigger slack_correlation_preresolution_gate
before insert on public.slack_correlation_notification_projection
for each row execute function public.enforce_slack_correlation_preresolution_gate();

update public.slack_correlation_notification_projection
set projection_status = 'suppressed',
    next_attempt_at = null,
    lease_owner = null,
    claim_token = null,
    lease_expires_at = null,
    last_failure_code = null,
    updated_at = clock_timestamp()
where notification_contract_version is null
  and attempt_count = 0
  and notification_id is null
  and projection_status in ('pending', 'retryable_failed', 'leased');

create or replace function public.claim_operator_correlation_preresolutions(
    p_tenant_ref text,
    p_funnel_ref text,
    p_worker_id text,
    p_lease_seconds integer default 120,
    p_binding_version integer default null
)
returns table (
    webhook_event_id uuid,
    source_event_type text,
    outcome text,
    claim_token uuid,
    lease_generation bigint
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := clock_timestamp();
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_worker_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 200
       or p_lease_seconds is null or p_lease_seconds < 30 or p_lease_seconds > 900
       or (p_binding_version is not null and p_binding_version < 1) then
        raise exception using errcode = '22023',
            message = 'invalid_operator_correlation_preresolution_claim';
    end if;

    if p_binding_version is not null and not exists (
        select 1
        from public.commercial_ally_runtime_bindings binding
        where binding.tenant_ref = p_tenant_ref
          and binding.funnel_ref = p_funnel_ref
          and binding.binding_version = p_binding_version
          and binding.status = 'active'
    ) then
        raise exception using errcode = '55000',
            message = 'commercial_ally_binding_not_active';
    end if;

    insert into public.operator_correlation_preresolutions (
        webhook_event_id,
        tenant_ref,
        funnel_ref,
        source_event_type,
        outcome,
        reason_code,
        candidate_count
    )
    select
        correlation.webhook_event_id,
        scope.tenant_ref,
        scope.funnel_ref,
        correlation.event_type,
        correlation.outcome,
        correlation.reason_code,
        correlation.candidate_count
    from public.hotmart_purchase_intent_correlations correlation
    join public.hotmart_purchase_intent_scopes scope
      on scope.id = correlation.scope_id
    where scope.tenant_ref = p_tenant_ref
      and scope.funnel_ref = p_funnel_ref
      and correlation.manual_handoff_required
      and correlation.purchase_intent_id is null
      and correlation.outcome in ('unmatched', 'ambiguous', 'conflict')
      and correlation.event_type in (
          'PURCHASE_APPROVED', 'PURCHASE_OUT_OF_SHOPPING_CART'
      )
      and not exists (
          select 1
          from public.operator_correlation_resolutions resolution
          where resolution.webhook_event_id = correlation.webhook_event_id
      )
      and not exists (
          select 1
          from public.slack_correlation_notification_projection projection
          where projection.source_event_id = correlation.webhook_event_id
            and (
                projection.attempt_count > 0
                or projection.notification_id is not null
                or projection.notification_contract_version in (1, 2)
            )
      )
    on conflict on constraint operator_correlation_preresolutions_pkey do nothing;

    return query
    with candidate as (
        select pre.webhook_event_id, pre.preresolution_version
        from public.operator_correlation_preresolutions pre
        where pre.tenant_ref = p_tenant_ref
          and pre.funnel_ref = p_funnel_ref
          and pre.preresolution_version = 1
          and pre.status in ('pending', 'retryable_failed', 'leased')
          and (pre.status <> 'retryable_failed' or pre.next_attempt_at <= v_now)
          and (pre.status <> 'leased' or pre.lease_expires_at <= v_now)
        order by pre.created_at, pre.webhook_event_id
        for update of pre skip locked
        limit 1
    ), claimed as (
        update public.operator_correlation_preresolutions pre
        set status = 'leased',
            lease_owner = p_worker_id,
            claim_token = gen_random_uuid(),
            lease_generation = pre.lease_generation + 1,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
            attempt_count = pre.attempt_count + 1,
            next_attempt_at = null,
            last_failure_code = null,
            updated_at = v_now
        from candidate
        where pre.webhook_event_id = candidate.webhook_event_id
          and pre.preresolution_version = candidate.preresolution_version
        returning pre.*
    )
    select
        claimed.webhook_event_id,
        claimed.source_event_type,
        claimed.outcome,
        claimed.claim_token,
        claimed.lease_generation
    from claimed;
end;
$function$;

create or replace function public.get_operator_correlation_preresolution_evidence(
    p_tenant_ref text,
    p_funnel_ref text,
    p_webhook_event_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint
)
returns table (evidence_data jsonb)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_pre public.operator_correlation_preresolutions%rowtype;
    v_now timestamptz := clock_timestamp();
    v_evidence jsonb;
begin
    select pre.* into v_pre
    from public.operator_correlation_preresolutions pre
    where pre.webhook_event_id = p_webhook_event_id
      and pre.preresolution_version = 1
      and pre.tenant_ref = p_tenant_ref
      and pre.funnel_ref = p_funnel_ref;

    if not found
       or v_pre.status <> 'leased'
       or v_pre.claim_token is distinct from p_claim_token
       or v_pre.lease_generation is distinct from p_lease_generation
       or v_pre.lease_expires_at <= v_now then
        raise exception using errcode = '55000',
            message = 'operator_correlation_preresolution_lease_lost';
    end if;

    if v_pre.evidence_snapshot is not null then
        return query select v_pre.evidence_snapshot;
        return;
    end if;

    with raw_candidates as (
        select
            candidate.purchase_intent_id,
            candidate.email_match,
            candidate.phone_match,
            row_number() over (order by candidate.purchase_intent_id) as ordinal,
            case
                when intent.submitted_at <= correlation.observed_at then
                    floor(extract(epoch from (
                        correlation.observed_at - intent.submitted_at
                    )) / 60)::integer
                else null
            end as gap_minutes
        from public.hotmart_purchase_intent_correlation_candidates candidate
        join public.hotmart_purchase_intent_correlations correlation
          on correlation.webhook_event_id = candidate.webhook_event_id
        join public.purchase_intents intent
          on intent.id = candidate.purchase_intent_id
        where candidate.webhook_event_id = p_webhook_event_id
    ), ranked as (
        select
            raw.*,
            row_number() over (
                order by raw.gap_minutes nulls last, raw.purchase_intent_id
            ) as gap_rank,
            lead(raw.gap_minutes) over (
                order by raw.gap_minutes nulls last, raw.purchase_intent_id
            ) as next_gap_minutes
        from raw_candidates raw
    ), candidate_json as (
        select coalesce(jsonb_agg(
            jsonb_build_object(
                'candidate_id', ranked.purchase_intent_id,
                'label', 'Persona ' || ranked.ordinal::text
            ) order by ranked.ordinal
        ), '[]'::jsonb) as value
        from ranked
    ), fact_rows as (
        select
            'fact-email-' || ranked.purchase_intent_id::text as evidence_id,
            ranked.purchase_intent_id as candidate_id,
            'event_email_exact_match'::text as kind,
            null::integer as value,
            false as independent,
            false as discriminating
        from ranked where ranked.email_match
        union all
        select
            'fact-phone-' || ranked.purchase_intent_id::text,
            ranked.purchase_intent_id,
            'event_phone_exact_match',
            null::integer,
            false,
            false
        from ranked where ranked.phone_match
        union all
        select
            'fact-offer-' || ranked.purchase_intent_id::text,
            ranked.purchase_intent_id,
            'same_product_offer',
            null::integer,
            false,
            false
        from ranked
        union all
        select
            'fact-time-' || ranked.purchase_intent_id::text,
            ranked.purchase_intent_id,
            'precheckout_time_proximity_minutes',
            ranked.gap_minutes,
            true,
            (
                ranked.gap_rank = 1
                and ranked.gap_minutes between 0 and 15
                and ranked.next_gap_minutes is not null
                and ranked.next_gap_minutes - ranked.gap_minutes >= 5
            )
        from ranked
        where ranked.gap_minutes between 0 and 1440
    ), fact_json as (
        select coalesce(jsonb_agg(
            jsonb_build_object(
                'evidence_id', facts.evidence_id,
                'candidate_id', facts.candidate_id,
                'kind', facts.kind,
                'value', facts.value,
                'independent', facts.independent,
                'discriminating', facts.discriminating
            ) order by facts.evidence_id
        ), '[]'::jsonb) as value
        from fact_rows facts
    )
    select jsonb_build_object(
        'case_id', v_pre.webhook_event_id,
        'event_type', v_pre.source_event_type,
        'outcome', v_pre.outcome,
        'candidates', candidate_json.value,
        'facts', fact_json.value
    ) into v_evidence
    from candidate_json cross join fact_json;

    if jsonb_array_length(v_evidence -> 'candidates') <> v_pre.candidate_count then
        raise exception using errcode = '55000',
            message = 'operator_correlation_preresolution_candidate_drift';
    end if;

    update public.operator_correlation_preresolutions pre
    set evidence_snapshot = v_evidence,
        evidence_fingerprint = encode(
            sha256(convert_to(v_evidence::text, 'UTF8')), 'hex'
        ),
        updated_at = v_now
    where pre.webhook_event_id = p_webhook_event_id
      and pre.preresolution_version = 1
      and pre.status = 'leased'
      and pre.claim_token = p_claim_token
      and pre.lease_generation = p_lease_generation
      and pre.evidence_snapshot is null;

    if not found then
        raise exception using errcode = '55000',
            message = 'operator_correlation_preresolution_snapshot_conflict';
    end if;

    return query select v_evidence;
end;
$function$;

create or replace function public.complete_operator_correlation_preresolution(
    p_webhook_event_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint,
    p_disposition text,
    p_recommended_purchase_intent_id uuid default null,
    p_supporting_evidence jsonb default '[]'::jsonb,
    p_model_name text default null,
    p_prompt_version text default null
)
returns table (
    recommendation_ref uuid,
    status text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_pre public.operator_correlation_preresolutions%rowtype;
    v_now timestamptz := clock_timestamp();
    v_recommendation_ref uuid;
    v_item jsonb;
begin
    select pre.* into v_pre
    from public.operator_correlation_preresolutions pre
    where pre.webhook_event_id = p_webhook_event_id
      and pre.preresolution_version = 1
    for update;

    if not found
       or v_pre.status <> 'leased'
       or v_pre.claim_token is distinct from p_claim_token
       or v_pre.lease_generation is distinct from p_lease_generation
       or v_pre.lease_expires_at <= v_now then
        raise exception using errcode = '55000',
            message = 'operator_correlation_preresolution_lease_lost';
    end if;

    if p_disposition not in ('recommended', 'abstained', 'suppressed_unmatched')
       or jsonb_typeof(coalesce(p_supporting_evidence, 'null'::jsonb)) <> 'array'
       or jsonb_array_length(p_supporting_evidence) > 5 then
        raise exception using errcode = '22023',
            message = 'invalid_operator_correlation_preresolution_completion';
    end if;

    if p_disposition = 'suppressed_unmatched' then
        if v_pre.outcome <> 'unmatched'
           or p_recommended_purchase_intent_id is not null
           or p_supporting_evidence <> '[]'::jsonb then
            raise exception using errcode = '22023',
                message = 'invalid_operator_correlation_preresolution_suppression';
        end if;
    elsif p_disposition = 'abstained' then
        if v_pre.outcome not in ('ambiguous', 'conflict')
           or p_recommended_purchase_intent_id is not null
           or p_supporting_evidence <> '[]'::jsonb
           or p_model_name is null or nullif(btrim(p_model_name), '') is null
           or p_prompt_version is null or nullif(btrim(p_prompt_version), '') is null then
            raise exception using errcode = '22023',
                message = 'invalid_operator_correlation_preresolution_abstention';
        end if;
    else
        if v_pre.outcome not in ('ambiguous', 'conflict')
           or v_pre.evidence_snapshot is null
           or v_pre.evidence_fingerprint is null
           or p_recommended_purchase_intent_id is null
           or jsonb_array_length(p_supporting_evidence) < 1
           or p_model_name is null or nullif(btrim(p_model_name), '') is null
           or p_prompt_version is null or nullif(btrim(p_prompt_version), '') is null
           or char_length(p_model_name) > 200
           or char_length(p_prompt_version) > 200
           or not exists (
               select 1
               from public.hotmart_purchase_intent_correlation_candidates candidate
               where candidate.webhook_event_id = p_webhook_event_id
                 and candidate.purchase_intent_id = p_recommended_purchase_intent_id
           ) then
            raise exception using errcode = '22023',
                message = 'invalid_operator_correlation_preresolution_recommendation';
        end if;

        for v_item in select value from jsonb_array_elements(p_supporting_evidence)
        loop
            if jsonb_typeof(v_item) <> 'object'
               or (v_item - array['kind', 'value']) <> '{}'::jsonb
               or v_item ->> 'kind' not in (
                   'precheckout_time_proximity_minutes',
                   'nearest_precheckout_by_at_least_5m',
                   'same_product_offer',
                   'chatwoot_phone_exact_match',
                   'chatwoot_email_exact_match',
                   'customer_confirmed_identity',
                   'prior_verified_identity',
                   'event_email_exact_match',
                   'event_phone_exact_match'
               )
               or not exists (
                   select 1
                   from jsonb_array_elements(
                       v_pre.evidence_snapshot -> 'facts'
                   ) fact(value)
                   where fact.value ->> 'candidate_id' =
                           p_recommended_purchase_intent_id::text
                     and fact.value ->> 'kind' = v_item ->> 'kind'
                     and (fact.value -> 'value') is not distinct from
                         (v_item -> 'value')
               ) then
                raise exception using errcode = '22023',
                    message = 'invalid_operator_correlation_preresolution_evidence';
            end if;
        end loop;

        if not exists (
            select 1
            from jsonb_array_elements(p_supporting_evidence) supplied(value)
            join jsonb_array_elements(v_pre.evidence_snapshot -> 'facts') fact(value)
              on fact.value ->> 'candidate_id' =
                    p_recommended_purchase_intent_id::text
             and fact.value ->> 'kind' = supplied.value ->> 'kind'
             and (fact.value -> 'value') is not distinct from
                 (supplied.value -> 'value')
            where (fact.value ->> 'independent')::boolean
              and (fact.value ->> 'discriminating')::boolean
        ) then
            raise exception using errcode = '22023',
                message = 'operator_correlation_preresolution_evidence_not_discriminating';
        end if;

        v_recommendation_ref := gen_random_uuid();
    end if;

    update public.operator_correlation_preresolutions pre
    set status = p_disposition,
        recommendation_ref = v_recommendation_ref,
        recommended_purchase_intent_id = case
            when p_disposition = 'recommended'
            then p_recommended_purchase_intent_id
            else null
        end,
        supporting_evidence = case
            when p_disposition = 'recommended' then p_supporting_evidence
            else '[]'::jsonb
        end,
        model_name = case
            when p_disposition in ('recommended', 'abstained') then p_model_name
            else null
        end,
        prompt_version = case
            when p_disposition in ('recommended', 'abstained') then p_prompt_version
            else null
        end,
        decided_at = v_now,
        lease_owner = null,
        claim_token = null,
        lease_expires_at = null,
        updated_at = v_now
    where pre.webhook_event_id = p_webhook_event_id
      and pre.preresolution_version = 1;

    if p_disposition = 'recommended' then
        insert into public.slack_correlation_notification_projection (
            source_event_id,
            tenant_ref,
            funnel_ref,
            outcome,
            reason_code,
            candidate_count,
            occurred_at,
            projection_status,
            notification_contract_version
        )
        select
            pre.webhook_event_id,
            pre.tenant_ref,
            pre.funnel_ref,
            pre.outcome,
            pre.reason_code,
            pre.candidate_count,
            correlation.observed_at,
            'pending',
            3
        from public.operator_correlation_preresolutions pre
        join public.hotmart_purchase_intent_correlations correlation
          on correlation.webhook_event_id = pre.webhook_event_id
        where pre.webhook_event_id = p_webhook_event_id
          and pre.preresolution_version = 1
        on conflict (source_event_id) do update
        set projection_status = 'pending',
            notification_contract_version = 3,
            next_attempt_at = null,
            lease_owner = null,
            claim_token = null,
            lease_expires_at = null,
            last_failure_code = null,
            updated_at = v_now
        where slack_correlation_notification_projection.attempt_count = 0
          and slack_correlation_notification_projection.notification_id is null
          and slack_correlation_notification_projection.notification_contract_version is null
          and slack_correlation_notification_projection.projection_status = 'suppressed';
    end if;

    return query select v_recommendation_ref, p_disposition;
end;
$function$;

create or replace function public.release_operator_correlation_preresolution(
    p_webhook_event_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint,
    p_failure_code text
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := clock_timestamp();
begin
    if p_failure_code is null or p_failure_code !~ '^[a-z0-9_]{1,80}$' then
        raise exception using errcode = '22023',
            message = 'invalid_operator_correlation_preresolution_failure';
    end if;

    update public.operator_correlation_preresolutions pre
    set status = case when pre.attempt_count >= 5
            then 'terminal_failed' else 'retryable_failed' end,
        next_attempt_at = case when pre.attempt_count >= 5 then null
            else v_now + make_interval(secs => least(3600, 30 * (2 ^ least(pre.attempt_count, 6)))::integer)
        end,
        lease_owner = null,
        claim_token = null,
        lease_expires_at = null,
        last_failure_code = p_failure_code,
        updated_at = v_now
    where pre.webhook_event_id = p_webhook_event_id
      and pre.preresolution_version = 1
      and pre.status = 'leased'
      and pre.claim_token = p_claim_token
      and pre.lease_generation = p_lease_generation
      and pre.lease_expires_at > v_now;

    if not found then
        raise exception using errcode = '55000',
            message = 'operator_correlation_preresolution_lease_lost';
    end if;
end;
$function$;

-- After this migration V1/V2 only deliver historical contracts. The existing
-- projection step stays active so the gate can durably suppress or enqueue new
-- unresolved correlations, but legacy workers must never lease V3 work.
create or replace function public.claim_slack_correlation_notifications_v2(
    p_tenant_ref text,
    p_funnel_ref text,
    p_worker_id text,
    p_limit integer default 1,
    p_lease_seconds integer default 60,
    p_binding_version integer default null
)
returns table (
    source_event_id uuid,
    source_event_type text,
    notification_contract_version integer,
    outcome text,
    reason_code text,
    candidate_count integer,
    occurred_at timestamptz,
    claim_token uuid,
    lease_generation bigint
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := clock_timestamp();
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_worker_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 200
       or p_limit is distinct from 1
       or p_lease_seconds is null or p_lease_seconds < 30 or p_lease_seconds > 900
       or (p_binding_version is not null and p_binding_version < 1) then
        raise exception using errcode = '22023',
            message = 'invalid_slack_correlation_projection_claim';
    end if;

    if p_binding_version is not null and not exists (
        select 1
        from public.commercial_ally_runtime_bindings binding
        where binding.tenant_ref = p_tenant_ref
          and binding.funnel_ref = p_funnel_ref
          and binding.binding_version = p_binding_version
          and binding.status = 'active'
    ) then
        raise exception using errcode = '55000',
            message = 'commercial_ally_binding_not_active';
    end if;

    insert into public.slack_correlation_notification_projection (
        source_event_id,
        tenant_ref,
        funnel_ref,
        outcome,
        reason_code,
        candidate_count,
        occurred_at
    )
    select
        correlation.webhook_event_id,
        scope.tenant_ref,
        scope.funnel_ref,
        correlation.outcome,
        correlation.reason_code,
        correlation.candidate_count,
        correlation.observed_at
    from public.hotmart_purchase_intent_correlations correlation
    join public.hotmart_purchase_intent_scopes scope
      on scope.id = correlation.scope_id
    where scope.tenant_ref = p_tenant_ref
      and scope.funnel_ref = p_funnel_ref
      and correlation.manual_handoff_required
      and correlation.purchase_intent_id is null
      and correlation.outcome in ('unmatched', 'ambiguous', 'conflict')
      and not exists (
          select 1
          from public.operator_correlation_resolutions resolution
          where resolution.webhook_event_id = correlation.webhook_event_id
      )
    on conflict on constraint slack_correlation_notification_projection_pkey
    do nothing;

    return query
    with candidates as (
        select projection.source_event_id
        from public.slack_correlation_notification_projection projection
        join public.hotmart_purchase_intent_correlations correlation
          on correlation.webhook_event_id = projection.source_event_id
        join public.webhook_events event
          on event.id = projection.source_event_id
        where projection.tenant_ref = p_tenant_ref
          and projection.funnel_ref = p_funnel_ref
          and event.event_type = correlation.event_type
          and projection.notification_contract_version in (1, 2)
          and projection.projection_status in ('pending', 'retryable_failed', 'leased')
          and (projection.projection_status <> 'retryable_failed'
               or projection.next_attempt_at <= v_now)
          and (projection.projection_status <> 'leased'
               or projection.lease_expires_at <= v_now)
          and not exists (
              select 1
              from public.operator_correlation_resolutions resolution
              where resolution.webhook_event_id = projection.source_event_id
          )
        order by projection.occurred_at, projection.source_event_id
        for update of projection skip locked
        limit p_limit
    ), claimed as (
        update public.slack_correlation_notification_projection projection
        set projection_status = 'leased',
            lease_owner = p_worker_id,
            claim_token = gen_random_uuid(),
            lease_generation = projection.lease_generation + 1,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
            attempt_count = projection.attempt_count + 1,
            next_attempt_at = null,
            updated_at = v_now
        from candidates
        where projection.source_event_id = candidates.source_event_id
        returning projection.*
    )
    select
        claimed.source_event_id,
        event.event_type,
        claimed.notification_contract_version,
        claimed.outcome,
        claimed.reason_code,
        claimed.candidate_count,
        claimed.occurred_at,
        claimed.claim_token,
        claimed.lease_generation
    from claimed
    join public.webhook_events event on event.id = claimed.source_event_id
    order by claimed.occurred_at, claimed.source_event_id;
end;
$function$;

create or replace function public.claim_slack_correlation_notifications_v3(
    p_tenant_ref text,
    p_funnel_ref text,
    p_worker_id text,
    p_limit integer default 1,
    p_lease_seconds integer default 60,
    p_binding_version integer default null
)
returns table (
    source_event_id uuid,
    source_event_type text,
    notification_contract_version integer,
    outcome text,
    reason_code text,
    candidate_count integer,
    occurred_at timestamptz,
    claim_token uuid,
    lease_generation bigint,
    recommendation_data jsonb
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := clock_timestamp();
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_worker_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 200
       or p_limit is distinct from 1
       or p_lease_seconds is null or p_lease_seconds < 30 or p_lease_seconds > 900
       or (p_binding_version is not null and p_binding_version < 1) then
        raise exception using errcode = '22023',
            message = 'invalid_slack_correlation_projection_claim';
    end if;

    if p_binding_version is not null and not exists (
        select 1
        from public.commercial_ally_runtime_bindings binding
        where binding.tenant_ref = p_tenant_ref
          and binding.funnel_ref = p_funnel_ref
          and binding.binding_version = p_binding_version
          and binding.status = 'active'
    ) then
        raise exception using errcode = '55000',
            message = 'commercial_ally_binding_not_active';
    end if;

    return query
    with candidate as (
        select projection.source_event_id
        from public.slack_correlation_notification_projection projection
        join public.operator_correlation_preresolutions pre
          on pre.webhook_event_id = projection.source_event_id
         and pre.preresolution_version = 1
         and pre.status = 'recommended'
         and pre.evidence_snapshot is not null
         and pre.evidence_fingerprint = encode(
             sha256(convert_to(pre.evidence_snapshot::text, 'UTF8')),
             'hex'
         )
        where projection.tenant_ref = p_tenant_ref
          and projection.funnel_ref = p_funnel_ref
          and projection.notification_contract_version = 3
          and projection.projection_status in ('pending', 'retryable_failed', 'leased')
          and (projection.projection_status <> 'retryable_failed'
               or projection.next_attempt_at <= v_now)
          and (projection.projection_status <> 'leased'
               or projection.lease_expires_at <= v_now)
          and not exists (
              select 1 from public.operator_correlation_resolutions resolution
              where resolution.webhook_event_id = projection.source_event_id
          )
        order by projection.occurred_at, projection.source_event_id
        for update of projection skip locked
        limit p_limit
    ), claimed as (
        update public.slack_correlation_notification_projection projection
        set projection_status = 'leased',
            lease_owner = p_worker_id,
            claim_token = gen_random_uuid(),
            lease_generation = projection.lease_generation + 1,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
            attempt_count = projection.attempt_count + 1,
            next_attempt_at = null,
            updated_at = v_now
        from candidate
        where projection.source_event_id = candidate.source_event_id
        returning projection.*
    )
    select
        claimed.source_event_id,
        event.event_type,
        claimed.notification_contract_version,
        claimed.outcome,
        claimed.reason_code,
        claimed.candidate_count,
        claimed.occurred_at,
        claimed.claim_token,
        claimed.lease_generation,
        jsonb_build_object(
            'recommendation_ref', pre.recommendation_ref,
            'candidate_id', pre.recommended_purchase_intent_id,
            'candidate_label', snapshot_candidate.label,
            'evidence', pre.supporting_evidence,
            'evidence_fingerprint', pre.evidence_fingerprint,
            'model_name', pre.model_name,
            'prompt_version', pre.prompt_version
        )
    from claimed
    join public.webhook_events event on event.id = claimed.source_event_id
    join public.operator_correlation_preresolutions pre
      on pre.webhook_event_id = claimed.source_event_id
     and pre.preresolution_version = 1
    join lateral (
        select candidate.value ->> 'label' as label
        from jsonb_array_elements(pre.evidence_snapshot -> 'candidates') candidate(value)
        where candidate.value ->> 'candidate_id' =
              pre.recommended_purchase_intent_id::text
    ) snapshot_candidate on true
    order by claimed.occurred_at, claimed.source_event_id;
end;
$function$;

revoke all on table public.operator_correlation_preresolutions from public;
revoke all on function public.enforce_slack_correlation_preresolution_gate() from public;
revoke all on function public.claim_operator_correlation_preresolutions(
    text, text, text, integer, integer
) from public;
revoke all on function public.get_operator_correlation_preresolution_evidence(
    text, text, uuid, uuid, bigint
) from public;
revoke all on function public.complete_operator_correlation_preresolution(
    uuid, uuid, bigint, text, uuid, jsonb, text, text
) from public;
revoke all on function public.release_operator_correlation_preresolution(
    uuid, uuid, bigint, text
) from public;
revoke all on function public.claim_slack_correlation_notifications_v3(
    text, text, text, integer, integer, integer
) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        revoke all on table public.operator_correlation_preresolutions from anon;
        revoke execute on function public.enforce_slack_correlation_preresolution_gate()
        from anon;
        revoke execute on function public.claim_operator_correlation_preresolutions(
            text, text, text, integer, integer
        ) from anon;
        revoke execute on function public.get_operator_correlation_preresolution_evidence(
            text, text, uuid, uuid, bigint
        ) from anon;
        revoke execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text
        ) from anon;
        revoke execute on function public.release_operator_correlation_preresolution(
            uuid, uuid, bigint, text
        ) from anon;
        revoke execute on function public.claim_slack_correlation_notifications_v3(
            text, text, text, integer, integer, integer
        ) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke all on table public.operator_correlation_preresolutions from authenticated;
        revoke execute on function public.enforce_slack_correlation_preresolution_gate()
        from authenticated;
        revoke execute on function public.claim_operator_correlation_preresolutions(
            text, text, text, integer, integer
        ) from authenticated;
        revoke execute on function public.get_operator_correlation_preresolution_evidence(
            text, text, uuid, uuid, bigint
        ) from authenticated;
        revoke execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text
        ) from authenticated;
        revoke execute on function public.release_operator_correlation_preresolution(
            uuid, uuid, bigint, text
        ) from authenticated;
        revoke execute on function public.claim_slack_correlation_notifications_v3(
            text, text, text, integer, integer, integer
        ) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        revoke all on table public.operator_correlation_preresolutions
        from service_role;
        revoke execute on function public.enforce_slack_correlation_preresolution_gate()
        from service_role;
        grant execute on function public.claim_operator_correlation_preresolutions(
            text, text, text, integer, integer
        ) to service_role;
        grant execute on function public.get_operator_correlation_preresolution_evidence(
            text, text, uuid, uuid, bigint
        ) to service_role;
        grant execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text
        ) to service_role;
        grant execute on function public.release_operator_correlation_preresolution(
            uuid, uuid, bigint, text
        ) to service_role;
        grant execute on function public.claim_slack_correlation_notifications_v3(
            text, text, text, integer, integer, integer
        ) to service_role;
    end if;
end;
$roles$;

commit;
