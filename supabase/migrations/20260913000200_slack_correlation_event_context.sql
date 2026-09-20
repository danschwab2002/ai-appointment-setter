-- Preserve commercial event context without breaking rolling replays.
-- A durable contract binding prevents old replicas from reclaiming V2 payloads.

begin;

alter table public.slack_correlation_notification_projection
    add column notification_contract_version integer;

update public.slack_correlation_notification_projection
set notification_contract_version = 1
where attempt_count > 0
   or notification_id is not null;

alter table public.slack_correlation_notification_projection
    add constraint slack_correlation_notification_contract_version_check
    check (notification_contract_version in (1, 2));

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
          and (
              projection.notification_contract_version is null
              or projection.notification_contract_version = 1
          )
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
        set notification_contract_version = coalesce(
                projection.notification_contract_version, 1
            ),
            projection_status = 'leased',
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
        set notification_contract_version = coalesce(
                projection.notification_contract_version, 2
            ),
            projection_status = 'leased',
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
    join public.webhook_events event
      on event.id = claimed.source_event_id
    order by claimed.occurred_at, claimed.source_event_id;
end;
$function$;

revoke all on function public.claim_slack_correlation_notifications_v2(
    text, text, text, integer, integer, integer
) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        revoke execute on function public.claim_slack_correlation_notifications_v2(
            text, text, text, integer, integer, integer
        ) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke execute on function public.claim_slack_correlation_notifications_v2(
            text, text, text, integer, integer, integer
        ) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.claim_slack_correlation_notifications_v2(
            text, text, text, integer, integer, integer
        ) to service_role;
    end if;
end;
$roles$;

commit;
