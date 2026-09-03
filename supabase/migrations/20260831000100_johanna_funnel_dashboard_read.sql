-- Read-only, sanitary projection for the on-demand Johanna funnel artifact.

begin;

create or replace function public.read_johanna_funnel_dashboard_v1(
    p_cutoff timestamptz,
    p_window_days integer default 7
)
returns table (
    case_id uuid,
    case_type text,
    provenance text,
    stage text,
    commercial_outcome text,
    control_outcomes text[],
    created_at timestamptz,
    updated_at timestamptz,
    conversation_id uuid,
    chatwoot_conversation_id bigint,
    chatwoot_status text,
    attention_reasons text[]
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_cutoff is null
       or p_window_days is null
       or not (p_window_days between 1 and 31) then
        raise exception using
            errcode = '22023',
            message = 'invalid_johanna_funnel_dashboard_window';
    end if;

    return query
    select intent.id as case_id,
           case
               when submission.has_submission and correlation.has_correlation
                   then 'both'
               when submission.has_submission then 'precheckout_only'
               else 'hotmart_only'
           end as case_type,
           case
               when test_command.is_controlled_test then 'controlled_test'
               when correlation.only_simulator then 'simulator'
               else 'unknown'
           end as provenance,
           case
               when command.status is not null then command.status
               when timer.status = 'scheduled' and timer.due_at <= p_cutoff then 'due'
               when timer.status = 'scheduled' then 'timer_scheduled'
               when timer.status = 'completed' then 'reevaluated'
               when correlation.has_correlation then 'correlated'
               else 'intent_created'
           end as stage,
           case
               when intent.lifecycle_state = 'purchased' then 'purchased'
               else 'unknown'
           end as commercial_outcome,
           array_remove(array[
               case when command.status in ('delivery_unknown', 'failed', 'blocked')
                   then command.status end,
               case when handoff.status is not null
                   then 'handoff_' || handoff.status end,
               case when opt_out.has_opt_out then 'opt_out' end
           ]::text[], null) as control_outcomes,
           intent.created_at,
           intent.updated_at,
           null::uuid as conversation_id,
           command.chatwoot_conversation_id,
           null::text as chatwoot_status,
           array_remove(array[
               case
                   when not test_command.is_controlled_test
                    and not correlation.only_simulator
                       then 'provenance_unknown'
               end,
               case
                   when correlation.problem_outcome is not null
                       then 'correlation_' || correlation.problem_outcome
               end,
               case when command.status = 'delivery_unknown' then 'delivery_unknown' end,
               case
                   when handoff.status is not null
                    and handoff.status not in ('projected', 'completed')
                       then 'handoff_pending'
               end
           ]::text[], null) as attention_reasons
    from public.purchase_intents intent
    left join lateral (
        select exists (
            select 1
            from public.purchase_intent_submissions link
            where link.purchase_intent_id = intent.id
        ) as has_submission
    ) submission on true
    left join lateral (
        select count(*) > 0 as has_correlation,
               bool_and(event.source = 'simulator') as only_simulator,
               min(correlation_row.outcome) filter (
                   where correlation_row.outcome in ('unmatched', 'ambiguous', 'conflict')
               ) as problem_outcome
        from public.hotmart_purchase_intent_correlations correlation_row
        join public.webhook_events event
          on event.id = correlation_row.webhook_event_id
        where correlation_row.purchase_intent_id = intent.id
    ) correlation on true
    left join lateral (
        select timer_row.status, timer_row.due_at
        from public.hotmart_abandonment_reevaluations timer_row
        where timer_row.purchase_intent_id = intent.id
          and timer_row.created_at < p_cutoff
        order by timer_row.created_at desc, timer_row.id desc
        limit 1
    ) timer on true
    left join lateral (
        select command_row.status, command_row.chatwoot_conversation_id
        from public.johanna_abandonment_one_shot_commands command_row
        where command_row.purchase_intent_id = intent.id
          and command_row.created_at < p_cutoff
        order by command_row.created_at desc, command_row.id desc
        limit 1
    ) command on true
    left join lateral (
        select handoff_row.status
        from public.human_handoff_requests handoff_row
        where handoff_row.external_conversation_id = command.chatwoot_conversation_id
          and handoff_row.created_at < p_cutoff
        order by handoff_row.created_at desc, handoff_row.id desc
        limit 1
    ) handoff on true
    left join lateral (
        select exists (
            select 1
            from public.contact_opt_out_events opt_out_row
            where opt_out_row.canonical_conversation_id = command.chatwoot_conversation_id
              and opt_out_row.correlation_status = 'applied'
              and opt_out_row.occurred_at >= intent.created_at
              and opt_out_row.occurred_at < p_cutoff
        ) as has_opt_out
    ) opt_out on true
    left join lateral (
        select exists (
            select 1
            from public.precheckout_test_first_touch_commands test_row
            where test_row.purchase_intent_id = intent.id
              and test_row.test_only
              and test_row.created_at < p_cutoff
        ) as is_controlled_test
    ) test_command on true
    where intent.created_at >= p_cutoff - make_interval(days => p_window_days)
      and intent.created_at < p_cutoff

    union all

    select commercial_case.id as case_id,
           'inbound'::text as case_type,
           'unknown'::text as provenance,
           commercial_case.status as stage,
           'unknown'::text as commercial_outcome,
           array_remove(array[
               case
                   when handoff.status is not null then 'handoff_' || handoff.status
               end,
               case when opt_out.has_opt_out then 'opt_out' end
           ]::text[], null) as control_outcomes,
           commercial_case.created_at,
           commercial_case.updated_at,
           commercial_case.conversation_id,
           admission.external_conversation_id as chatwoot_conversation_id,
           null::text as chatwoot_status,
           array_remove(array[
               'provenance_unknown',
               case
                   when commercial_case.identity_resolution_status in (
                       'ambiguous', 'conflict', 'unmatched'
                   ) then 'identity_' || commercial_case.identity_resolution_status
               end,
               case
                   when handoff.status is not null
                    and handoff.status not in ('projected', 'completed')
                       then 'handoff_pending'
               end
           ]::text[], null) as attention_reasons
    from public.commercial_cases commercial_case
    left join public.inbound_commercial_case_admissions admission
      on admission.commercial_case_id = commercial_case.id
     and admission.created_at < p_cutoff
    left join lateral (
        select handoff_row.status
        from public.human_handoff_requests handoff_row
        where handoff_row.commercial_case_id = commercial_case.id
          and handoff_row.created_at < p_cutoff
        order by handoff_row.created_at desc, handoff_row.id desc
        limit 1
    ) handoff on true
    left join lateral (
        select exists (
            select 1
            from public.contact_opt_out_events opt_out_row
            where opt_out_row.canonical_conversation_id = admission.external_conversation_id
              and opt_out_row.correlation_status = 'applied'
              and opt_out_row.occurred_at >= commercial_case.created_at
              and opt_out_row.occurred_at < p_cutoff
        ) as has_opt_out
    ) opt_out on true
    where commercial_case.case_kind = 'inbound_sales'
      and commercial_case.created_at >= p_cutoff - make_interval(days => p_window_days)
      and commercial_case.created_at < p_cutoff

    union all

    select payment.id as case_id,
           'payment_failure'::text as case_type,
           'unknown'::text as provenance,
           coalesce(command.status, payment.case_status) as stage,
           case
               when intent.lifecycle_state = 'purchased' then 'purchased'
               else 'unknown'
           end as commercial_outcome,
           array_remove(array[
               case when command.status in ('delivery_unknown', 'failed', 'blocked')
                   then command.status end,
               case when handoff.status is not null
                   then 'handoff_' || handoff.status end,
               case when opt_out.has_opt_out then 'opt_out' end
           ]::text[], null) as control_outcomes,
           payment.created_at,
           greatest(payment.observed_at, payment.created_at) as updated_at,
           null::uuid as conversation_id,
           command.chatwoot_conversation_id,
           null::text as chatwoot_status,
           array_remove(array[
               'provenance_unknown',
               case
                   when payment.correlation_outcome in ('unmatched', 'ambiguous', 'conflict')
                       then 'correlation_' || payment.correlation_outcome
               end,
               case when command.status = 'delivery_unknown' then 'delivery_unknown' end,
               case
                   when handoff.status is not null
                    and handoff.status not in ('projected', 'completed')
                       then 'handoff_pending'
               end
           ]::text[], null) as attention_reasons
    from public.johanna_payment_failure_cases payment
    left join public.purchase_intents intent
      on intent.id = payment.purchase_intent_id
    left join lateral (
        select command_row.status, command_row.chatwoot_conversation_id
        from public.johanna_abandonment_one_shot_commands command_row
        where command_row.payment_failure_case_id = payment.id
          and command_row.created_at < p_cutoff
        order by command_row.created_at desc, command_row.id desc
        limit 1
    ) command on true
    left join lateral (
        select handoff_row.status
        from public.human_handoff_requests handoff_row
        where handoff_row.external_conversation_id = command.chatwoot_conversation_id
          and handoff_row.created_at < p_cutoff
        order by handoff_row.created_at desc, handoff_row.id desc
        limit 1
    ) handoff on true
    left join lateral (
        select exists (
            select 1
            from public.contact_opt_out_events opt_out_row
            where opt_out_row.canonical_conversation_id = command.chatwoot_conversation_id
              and opt_out_row.correlation_status = 'applied'
              and opt_out_row.occurred_at >= payment.created_at
              and opt_out_row.occurred_at < p_cutoff
        ) as has_opt_out
    ) opt_out on true
    where payment.created_at >= p_cutoff - make_interval(days => p_window_days)
      and payment.created_at < p_cutoff

    order by created_at desc, case_id;
end;
$function$;

revoke all on function public.read_johanna_funnel_dashboard_v1(
    timestamptz, integer
) from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.read_johanna_funnel_dashboard_v1(
            timestamptz, integer
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.read_johanna_funnel_dashboard_v1(
            timestamptz, integer
        ) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.read_johanna_funnel_dashboard_v1(
            timestamptz, integer
        ) to service_role;
    end if;
end;
$acl$;

commit;
