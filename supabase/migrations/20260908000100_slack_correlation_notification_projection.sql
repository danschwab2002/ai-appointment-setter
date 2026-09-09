-- Durable, scoped projection of unresolved Hotmart correlations to Slack.

create table public.slack_correlation_notification_projection (
    source_event_id uuid primary key
        references public.hotmart_purchase_intent_correlations(webhook_event_id)
        on delete restrict,
    tenant_ref text not null,
    funnel_ref text not null,
    outcome text not null,
    reason_code text not null,
    candidate_count integer not null,
    occurred_at timestamptz not null,
    projection_status text not null default 'pending',
    attempt_count integer not null default 0,
    next_attempt_at timestamptz,
    lease_owner text,
    claim_token uuid,
    lease_generation bigint not null default 0,
    lease_expires_at timestamptz,
    notification_id uuid,
    last_failure_code text,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    check (nullif(btrim(tenant_ref), '') is not null),
    check (nullif(btrim(funnel_ref), '') is not null),
    check (outcome in ('unmatched', 'ambiguous', 'conflict')),
    check (nullif(btrim(reason_code), '') is not null),
    check (
        (outcome = 'unmatched' and candidate_count = 0)
        or (outcome = 'ambiguous' and candidate_count > 1)
        or (outcome = 'conflict' and candidate_count > 0)
    ),
    check (projection_status in (
        'pending', 'leased', 'retryable_failed', 'admitted', 'terminal_failed'
    )),
    check (attempt_count >= 0),
    check (lease_generation >= 0),
    check (
        (projection_status = 'leased'
            and lease_owner is not null
            and claim_token is not null
            and lease_expires_at is not null)
        or (projection_status <> 'leased'
            and lease_owner is null
            and claim_token is null
            and lease_expires_at is null)
    ),
    check (
        (projection_status = 'admitted' and notification_id is not null)
        or (projection_status <> 'admitted' and notification_id is null)
    )
);

create index slack_correlation_notification_projection_claim_idx
    on public.slack_correlation_notification_projection (
        tenant_ref,
        funnel_ref,
        projection_status,
        next_attempt_at,
        occurred_at,
        source_event_id
    );

alter table public.slack_correlation_notification_projection enable row level security;

create or replace function public.claim_slack_correlation_notifications(
    p_tenant_ref text,
    p_funnel_ref text,
    p_worker_id text,
    p_limit integer default 1,
    p_lease_seconds integer default 60,
    p_binding_version integer default null
)
returns table (
    source_event_id uuid,
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
        where projection.tenant_ref = p_tenant_ref
          and projection.funnel_ref = p_funnel_ref
          and projection.projection_status in ('pending', 'retryable_failed', 'leased')
          and (
              projection.projection_status <> 'retryable_failed'
              or projection.next_attempt_at <= v_now
          )
          and (
              projection.projection_status <> 'leased'
              or projection.lease_expires_at <= v_now
          )
          and not exists (
              select 1
              from public.operator_correlation_resolutions resolution
              where resolution.webhook_event_id = projection.source_event_id
          )
        order by projection.occurred_at, projection.source_event_id
        for update of projection skip locked
        limit p_limit
    ),
    claimed as (
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
        claimed.outcome,
        claimed.reason_code,
        claimed.candidate_count,
        claimed.occurred_at,
        claimed.claim_token,
        claimed.lease_generation
    from claimed
    order by claimed.occurred_at, claimed.source_event_id;
end;
$function$;

create or replace function public.complete_slack_correlation_notification(
    p_source_event_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint,
    p_notification_id uuid
)
returns table (applied boolean)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_source_event_id is null or p_claim_token is null
       or p_lease_generation is null or p_lease_generation < 1
       or p_notification_id is null then
        raise exception using errcode = '22023',
            message = 'invalid_slack_correlation_projection_completion';
    end if;

    return query
    with changed as (
        update public.slack_correlation_notification_projection projection
        set projection_status = 'admitted',
            notification_id = p_notification_id,
            last_failure_code = null,
            lease_owner = null,
            claim_token = null,
            lease_expires_at = null,
            next_attempt_at = null,
            updated_at = clock_timestamp()
        where projection.source_event_id = p_source_event_id
          and projection.projection_status = 'leased'
          and projection.claim_token = p_claim_token
          and projection.lease_generation = p_lease_generation
        returning 1
    )
    select exists(select 1 from changed);
end;
$function$;

create or replace function public.release_slack_correlation_notification(
    p_source_event_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint,
    p_failure_code text
)
returns table (applied boolean)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_source_event_id is null or p_claim_token is null
       or p_lease_generation is null or p_lease_generation < 1
       or p_failure_code not in (
           'connector_admission_unknown',
           'connector_semantic_conflict',
           'connector_rejected',
           'connector_unexpected_error'
       ) then
        raise exception using errcode = '22023',
            message = 'invalid_slack_correlation_projection_release';
    end if;

    return query
    with changed as (
        update public.slack_correlation_notification_projection projection
        set projection_status = case
                when p_failure_code = 'connector_admission_unknown'
                     and projection.attempt_count < 8
                    then 'retryable_failed'
                else 'terminal_failed'
            end,
            next_attempt_at = case
                when p_failure_code = 'connector_admission_unknown'
                     and projection.attempt_count < 8
                    then clock_timestamp() + make_interval(
                        secs => least(300, power(2, least(projection.attempt_count, 8))::integer)
                    )
                else null
            end,
            last_failure_code = p_failure_code,
            lease_owner = null,
            claim_token = null,
            lease_expires_at = null,
            updated_at = clock_timestamp()
        where projection.source_event_id = p_source_event_id
          and projection.projection_status = 'leased'
          and projection.claim_token = p_claim_token
          and projection.lease_generation = p_lease_generation
        returning 1
    )
    select exists(select 1 from changed);
end;
$function$;

revoke all on table public.slack_correlation_notification_projection from public;
revoke execute on function public.claim_slack_correlation_notifications(text, text, text, integer, integer, integer) from public;
revoke execute on function public.complete_slack_correlation_notification(uuid, uuid, bigint, uuid) from public;
revoke execute on function public.release_slack_correlation_notification(uuid, uuid, bigint, text) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        revoke all on table public.slack_correlation_notification_projection from anon;
        revoke execute on function public.claim_slack_correlation_notifications(text, text, text, integer, integer, integer) from anon;
        revoke execute on function public.complete_slack_correlation_notification(uuid, uuid, bigint, uuid) from anon;
        revoke execute on function public.release_slack_correlation_notification(uuid, uuid, bigint, text) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke all on table public.slack_correlation_notification_projection from authenticated;
        revoke execute on function public.claim_slack_correlation_notifications(text, text, text, integer, integer, integer) from authenticated;
        revoke execute on function public.complete_slack_correlation_notification(uuid, uuid, bigint, uuid) from authenticated;
        revoke execute on function public.release_slack_correlation_notification(uuid, uuid, bigint, text) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        revoke all on table public.slack_correlation_notification_projection from service_role;
        grant execute on function public.claim_slack_correlation_notifications(text, text, text, integer, integer, integer) to service_role;
        grant execute on function public.complete_slack_correlation_notification(uuid, uuid, bigint, uuid) to service_role;
        grant execute on function public.release_slack_correlation_notification(uuid, uuid, bigint, text) to service_role;
    end if;
end;
$roles$;
