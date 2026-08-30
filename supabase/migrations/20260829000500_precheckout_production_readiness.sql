-- Publish the dedicated precheckout authority boundary and expose sanitized
-- readiness. This migration does not enable scheduling, workers, or outbound.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $preflight$
declare
    v_steps jsonb;
begin
    if to_regprocedure(
        'public.schedule_precheckout_first_touch_reevaluation(uuid,uuid)'
    ) is null
       or to_regprocedure(
        'public.reevaluate_hotmart_abandonment_timer(uuid,timestamptz)'
       ) is null
       or to_regprocedure(
        'public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)'
       ) is null
       or to_regprocedure(
        'public.get_precheckout_delayed_one_shot_command(uuid)'
       ) is null then
        raise exception using errcode = '55000',
            message = 'precheckout_production_readiness_physical_stack_missing';
    end if;

    select policy.steps into v_steps
    from public.followup_policy_versions policy
    where policy.policy_key = 'johanna-abandonment-single-touch-e2e'
      and policy.version = 2
      and policy.status = 'published'
      and policy.purpose = 'cart_recovery'
      and policy.max_automatic_messages = 1;
    if not found
       or jsonb_typeof(v_steps) is distinct from 'array'
       or jsonb_array_length(v_steps) <> 1
       or v_steps #>> '{0,step_key}' is distinct from 'first_contact'
       or v_steps #>> '{0,mode}' is distinct from 'approved_template' then
        raise exception using errcode = '55000',
            message = 'precheckout_production_readiness_policy_missing';
    end if;

    if exists (
        select 1
        from public.hotmart_abandonment_timer_policy_bindings binding
        where binding.tenant_ref = 'lancemos'
          and binding.funnel_ref = 'psicologajohanna'
          and lower(binding.product_ref) = lower('F106691755G')
          and binding.offer_ref = 'bxjge6zq'
          and not (
              binding.enabled
              and not binding.precheckout_first_touch_enabled
              and (
                  (binding.policy_key = 'lancemos-johanna-abandonment-reevaluation'
                   and binding.policy_version = 1)
                  or
                  (binding.policy_key = 'johanna-precheckout-delayed-first-touch-timer'
                   and binding.policy_version = 1)
              )
          )
    ) then
        raise exception using errcode = '55000',
            message = 'precheckout_production_readiness_binding_not_default_off';
    end if;

    if exists (
        select 1 from public.pilot_scope_versions
        where scope_key = 'johanna-precheckout-delayed-first-touch'
    ) or exists (
        select 1 from public.pilot_runtime_controls
        where scope_key = 'johanna-precheckout-delayed-first-touch'
    ) then
        raise exception using errcode = '55000',
            message = 'precheckout_production_readiness_scope_already_present';
    end if;

    if exists (
        select 1 from public.hotmart_abandonment_reevaluations
        where source_kind = 'precheckout_intent'
    ) or exists (
        select 1 from public.johanna_abandonment_one_shot_commands
        where source_reevaluation_id is not null
    ) then
        raise exception using errcode = '55000',
            message = 'precheckout_production_readiness_backlog_not_zero';
    end if;
end;
$preflight$;

insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
)
select
    'johanna-precheckout-delayed-first-touch-timer', 1, 'published',
    source_policy.purpose, source_policy.timezone,
    source_policy.business_windows, interval '60 minutes',
    source_policy.expires_after, 1, source_policy.steps,
    'operator-authorized-mvp-scope-20260829',
    clock_timestamp(), clock_timestamp()
from public.followup_policy_versions source_policy
where source_policy.policy_key = 'johanna-abandonment-single-touch-e2e'
  and source_policy.version = 2
  and not exists (
      select 1
      from public.followup_policy_versions existing_policy
      where existing_policy.policy_key =
              'johanna-precheckout-delayed-first-touch-timer'
        and existing_policy.version = 1
  );

update public.hotmart_abandonment_timer_policy_bindings binding
set policy_key = 'johanna-precheckout-delayed-first-touch-timer',
    policy_version = 1,
    precheckout_first_touch_enabled = false,
    generation = binding.generation + 1,
    updated_at = clock_timestamp()
where binding.tenant_ref = 'lancemos'
  and binding.funnel_ref = 'psicologajohanna'
  and lower(binding.product_ref) = lower('F106691755G')
  and binding.offer_ref = 'bxjge6zq'
  and binding.enabled
  and not binding.precheckout_first_touch_enabled
  and binding.policy_key = 'lancemos-johanna-abandonment-reevaluation'
  and binding.policy_version = 1;

insert into public.hotmart_abandonment_timer_policy_bindings (
    tenant_ref, funnel_ref, product_ref, offer_ref, enabled,
    precheckout_first_touch_enabled, policy_key, policy_version
)
select
    'lancemos', 'psicologajohanna', 'F106691755G', 'bxjge6zq', true,
    false, 'johanna-precheckout-delayed-first-touch-timer', 1
where not exists (
    select 1
    from public.hotmart_abandonment_timer_policy_bindings binding
    where binding.tenant_ref = 'lancemos'
      and binding.funnel_ref = 'psicologajohanna'
      and lower(binding.product_ref) = lower('F106691755G')
      and binding.offer_ref = 'bxjge6zq'
);

insert into public.pilot_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, channel, channel_provider, channel_account_ref,
    source, source_event_type, external_product_id, offer_code, purpose,
    policy_key, policy_version, timezone, max_cohort_contacts,
    max_outbound_request_starts_total, max_outbound_request_starts_per_day,
    approved_by, approved_at, published_at
) values (
    'johanna-precheckout-delayed-first-touch', 1, 'published',
    'lancemos', 1, 9, 'whatsapp', 'waba', 'chatwoot-inbox:9',
    'landing', 'PRECHECKOUT_FORM_SUBMITTED', 'F106691755G', 'bxjge6zq',
    'cart_recovery', 'johanna-abandonment-single-touch-e2e', 2,
    'UTC', 1, 1, 1,
    'operator-authorized-mvp-scope-20260829',
    clock_timestamp(), clock_timestamp()
);

insert into public.pilot_runtime_controls (
    scope_key, scope_version, runtime_state, generation,
    changed_by, change_reason
) values (
    'johanna-precheckout-delayed-first-touch', 1, 'inactive', 0,
    'operator-authorized-mvp-scope-20260829',
    'Publish precheckout authority only; timer binding and process effects remain default-off.'
);

create or replace function public.get_precheckout_delayed_first_touch_readiness()
returns table (
    migration_tracking_complete boolean,
    scope_configured boolean,
    runtime_state text,
    runtime_generation bigint,
    timer_binding_enabled boolean,
    timer_binding_generation bigint,
    first_touch_binding_enabled boolean,
    due_count bigint,
    reserved_count bigint,
    request_started_count bigint,
    delivery_unknown_count bigint,
    reason_code text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_tracking_complete boolean := false;
    v_scope_configured boolean := false;
    v_runtime_state text;
    v_runtime_generation bigint;
    v_timer_binding_enabled boolean := false;
    v_timer_binding_generation bigint;
    v_first_touch_binding_enabled boolean := false;
    v_timer_binding_policy_matches boolean := false;
    v_due_count bigint := 0;
    v_reserved_count bigint := 0;
    v_request_started_count bigint := 0;
    v_delivery_unknown_count bigint := 0;
    v_reason_code text;
begin
    if to_regclass('supabase_migrations.schema_migrations') is not null then
        execute $tracking$
            select count(*) = 4
            from supabase_migrations.schema_migrations
            where version in (
                '20260829000200',
                '20260829000300',
                '20260829000400',
                '20260829000500'
            )
        $tracking$ into v_tracking_complete;
    end if;

    select exists (
        select 1
        from public.pilot_scope_versions scope
        where scope.scope_key = 'johanna-precheckout-delayed-first-touch'
          and scope.version = 1
          and scope.status = 'published'
          and scope.tenant_key = 'lancemos'
          and scope.chatwoot_account_id = 1
          and scope.chatwoot_inbox_id = 9
          and scope.channel = 'whatsapp'
          and scope.channel_provider = 'waba'
          and scope.channel_account_ref = 'chatwoot-inbox:9'
          and scope.source = 'landing'
          and scope.source_event_type = 'PRECHECKOUT_FORM_SUBMITTED'
          and scope.external_product_id = 'F106691755G'
          and scope.offer_code = 'bxjge6zq'
          and scope.purpose = 'cart_recovery'
          and scope.policy_key = 'johanna-abandonment-single-touch-e2e'
          and scope.policy_version = 2
          and scope.max_cohort_contacts = 1
          and scope.max_outbound_request_starts_total = 1
          and scope.max_outbound_request_starts_per_day = 1
    ) into v_scope_configured;

    select runtime.runtime_state, runtime.generation
    into v_runtime_state, v_runtime_generation
    from public.pilot_runtime_controls runtime
    where runtime.scope_key = 'johanna-precheckout-delayed-first-touch'
      and runtime.scope_version = 1;

    select binding.enabled, binding.generation,
           binding.precheckout_first_touch_enabled,
           binding.policy_key = 'johanna-precheckout-delayed-first-touch-timer'
             and binding.policy_version = 1
             and policy.status = 'published'
             and policy.grace_period = interval '60 minutes'
    into v_timer_binding_enabled, v_timer_binding_generation,
         v_first_touch_binding_enabled, v_timer_binding_policy_matches
    from public.hotmart_abandonment_timer_policy_bindings binding
    join public.followup_policy_versions policy
      on policy.policy_key = binding.policy_key
     and policy.version = binding.policy_version
    where binding.tenant_ref = 'lancemos'
      and binding.funnel_ref = 'psicologajohanna'
      and lower(binding.product_ref) = lower('F106691755G')
      and binding.offer_ref = 'bxjge6zq';

    select count(*) into v_due_count
    from public.hotmart_abandonment_reevaluations timer
    left join public.johanna_abandonment_one_shot_commands command
      on command.source_reevaluation_id = timer.id
    where timer.source_kind = 'precheckout_intent'
      and (
          (timer.status = 'scheduled' and timer.due_at <= clock_timestamp())
          or (
              timer.status = 'completed'
              and timer.outcome = 'command_reserved'
              and command.status in ('reserved', 'request_started')
          )
      );

    select count(*) filter (where command.status = 'reserved'),
           count(*) filter (where command.status = 'request_started'),
           count(*) filter (where command.status = 'delivery_unknown')
    into v_reserved_count, v_request_started_count, v_delivery_unknown_count
    from public.johanna_abandonment_one_shot_commands command
    where command.source_reevaluation_id is not null;

    v_reason_code := case
        when not v_tracking_complete then 'migration_tracking_incomplete'
        when not v_scope_configured then 'precheckout_scope_not_configured'
        when v_runtime_state is distinct from 'inactive'
          or v_runtime_generation is distinct from 0
            then 'precheckout_runtime_not_inactive'
        when not v_timer_binding_enabled then 'timer_binding_disabled'
        when not v_timer_binding_policy_matches
            then 'timer_binding_policy_mismatch'
        when not v_first_touch_binding_enabled then 'first_touch_binding_disabled'
        else 'precheckout_first_touch_ready'
    end;

    return query select
        v_tracking_complete,
        v_scope_configured,
        v_runtime_state,
        v_runtime_generation,
        v_timer_binding_enabled,
        v_timer_binding_generation,
        v_first_touch_binding_enabled,
        v_due_count,
        v_reserved_count,
        v_request_started_count,
        v_delivery_unknown_count,
        v_reason_code;
end;
$function$;

revoke all on function public.get_precheckout_delayed_first_touch_readiness() from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.get_precheckout_delayed_first_touch_readiness() from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.get_precheckout_delayed_first_touch_readiness() from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on function public.get_precheckout_delayed_first_touch_readiness() from service_role;
        grant execute on function public.get_precheckout_delayed_first_touch_readiness() to service_role;
    end if;
end;
$acl$;

do $postflight$
declare
    v_rpc regprocedure := to_regprocedure(
        'public.get_precheckout_delayed_first_touch_readiness()'
    );
begin
    if v_rpc is null
       or not exists (
            select 1
            from public.pilot_scope_versions scope
            join public.pilot_runtime_controls runtime
              on runtime.scope_key = scope.scope_key
             and runtime.scope_version = scope.version
            where scope.scope_key = 'johanna-precheckout-delayed-first-touch'
              and scope.version = 1
              and scope.status = 'published'
              and runtime.runtime_state = 'inactive'
              and runtime.generation = 0
       )
       or not exists (
            select 1
            from public.hotmart_abandonment_timer_policy_bindings binding
            join public.followup_policy_versions policy
              on policy.policy_key = binding.policy_key
             and policy.version = binding.policy_version
            where binding.tenant_ref = 'lancemos'
              and binding.funnel_ref = 'psicologajohanna'
              and lower(binding.product_ref) = lower('F106691755G')
              and binding.offer_ref = 'bxjge6zq'
              and binding.enabled
              and not binding.precheckout_first_touch_enabled
              and binding.policy_key =
                    'johanna-precheckout-delayed-first-touch-timer'
              and binding.policy_version = 1
              and policy.status = 'published'
              and policy.grace_period = interval '60 minutes'
       )
       or has_function_privilege('public', v_rpc, 'EXECUTE') then
        raise exception using errcode = '55000',
            message = 'precheckout_production_readiness_postflight_failed';
    end if;
end;
$postflight$;

commit;
