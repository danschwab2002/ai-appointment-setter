-- Sanitary browser-funnel observations and historically bounded dashboard V2.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

create table public.johanna_funnel_events (
    event_id text primary key,
    contract_version text not null,
    event_type text not null,
    occurred_at timestamptz not null,
    anonymous_session_id text not null,
    landing_ref text not null,
    offer_ref text not null,
    utm_source text,
    utm_medium text,
    utm_campaign text,
    utm_content text,
    utm_term text,
    admitted_at timestamptz not null default clock_timestamp(),
    foreign key (landing_ref, offer_ref)
        references public.johanna_precheckout_landing_offers (landing_ref, offer_ref)
        on delete restrict,
    check (contract_version = '1.0.0'),
    check (event_id ~ '^[0-9A-HJKMNP-TV-Z]{26}$'),
    check (anonymous_session_id ~ '^[0-9A-HJKMNP-TV-Z]{26}$'),
    check (event_type in (
        'page_view', 'preform_opened', 'preform_submitted', 'checkout_redirected'
    )),
    check (length(landing_ref) between 1 and 64),
    check (length(offer_ref) between 1 and 64),
    check (utm_source is null or length(utm_source) <= 128),
    check (utm_medium is null or length(utm_medium) <= 128),
    check (utm_campaign is null or length(utm_campaign) <= 128),
    check (utm_content is null or length(utm_content) <= 128),
    check (utm_term is null or length(utm_term) <= 128)
);

create index johanna_funnel_events_session_time_idx
on public.johanna_funnel_events (anonymous_session_id, occurred_at, event_id);

alter table public.johanna_funnel_events enable row level security;

create function public.johanna_funnel_events_append_only()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '55000',
        message = 'johanna_funnel_events_are_append_only';
end;
$function$;

create trigger johanna_funnel_events_append_only
before update or delete on public.johanna_funnel_events
for each row execute function public.johanna_funnel_events_append_only();

create or replace function public.admit_johanna_funnel_event_v1(
    p_version text,
    p_event_id text,
    p_event_type text,
    p_occurred_at timestamptz,
    p_anonymous_session_id text,
    p_landing_ref text,
    p_offer_ref text,
    p_utm_source text,
    p_utm_medium text,
    p_utm_campaign text,
    p_utm_content text,
    p_utm_term text
)
returns table (outcome text, event_id text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    existing public.johanna_funnel_events%rowtype;
begin
    if p_version is distinct from '1.0.0'
       or p_event_id is null
       or p_event_id !~ '^[0-9A-HJKMNP-TV-Z]{26}$'
       or p_event_type not in (
           'page_view', 'preform_opened', 'preform_submitted', 'checkout_redirected'
       )
       or p_occurred_at is null
       or p_occurred_at < statement_timestamp() - interval '31 days'
       or p_occurred_at > statement_timestamp() + interval '5 minutes'
       or p_anonymous_session_id is null
       or p_anonymous_session_id !~ '^[0-9A-HJKMNP-TV-Z]{26}$'
       or p_landing_ref is null or length(p_landing_ref) not between 1 and 64
       or p_offer_ref is null or length(p_offer_ref) not between 1 and 64
       or length(p_utm_source) > 128
       or length(p_utm_medium) > 128
       or length(p_utm_campaign) > 128
       or length(p_utm_content) > 128
       or length(p_utm_term) > 128
       or not public.is_johanna_precheckout_pair(p_landing_ref, p_offer_ref) then
        raise exception using errcode = '22023',
            message = 'invalid_johanna_funnel_event';
    end if;

    insert into public.johanna_funnel_events (
        event_id, contract_version, event_type, occurred_at,
        anonymous_session_id, landing_ref, offer_ref,
        utm_source, utm_medium, utm_campaign, utm_content, utm_term
    ) values (
        p_event_id, p_version, p_event_type, p_occurred_at,
        p_anonymous_session_id, p_landing_ref, p_offer_ref,
        p_utm_source, p_utm_medium, p_utm_campaign, p_utm_content, p_utm_term
    ) on conflict on constraint johanna_funnel_events_pkey do nothing;

    if found then
        return query select 'inserted'::text, p_event_id;
        return;
    end if;

    select stored.* into strict existing
    from public.johanna_funnel_events stored
    where stored.event_id = p_event_id;

    if existing.contract_version is not distinct from p_version
       and existing.event_type is not distinct from p_event_type
       and existing.occurred_at is not distinct from p_occurred_at
       and existing.anonymous_session_id is not distinct from p_anonymous_session_id
       and existing.landing_ref is not distinct from p_landing_ref
       and existing.offer_ref is not distinct from p_offer_ref
       and existing.utm_source is not distinct from p_utm_source
       and existing.utm_medium is not distinct from p_utm_medium
       and existing.utm_campaign is not distinct from p_utm_campaign
       and existing.utm_content is not distinct from p_utm_content
       and existing.utm_term is not distinct from p_utm_term then
        return query select 'duplicate'::text, p_event_id;
    else
        return query select 'semantic_conflict'::text, p_event_id;
    end if;
end;
$function$;

drop function if exists public.read_johanna_funnel_dashboard_v2(timestamptz, integer);

create or replace function public.read_johanna_funnel_dashboard_v2(
    p_window_days integer default 7
)
returns table (
    row_kind text,
    snapshot_at timestamptz,
    window_start timestamptz,
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
    attention_reasons text[],
    page_view_count bigint,
    preform_opened_count bigint,
    preform_submitted_count bigint,
    checkout_redirected_count bigint,
    last_funnel_event_at timestamptz,
    last_funnel_event_type text
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_snapshot_at timestamptz := statement_timestamp();
begin
    if p_window_days is null or not (p_window_days between 1 and 31) then
        raise exception using errcode = '22023',
            message = 'invalid_johanna_funnel_dashboard_window';
    end if;

    return query
    with dashboard_cases as (
    select intent.id,
           case when submission.has_submission and correlation.has_correlation then 'both'
                when submission.has_submission then 'precheckout_only'
                else 'hotmart_only' end,
           case when test_command.is_controlled_test then 'controlled_test'
                when correlation.only_simulator then 'simulator'
                else 'unknown' end,
           case when command.status is not null then command.status
                when timer.status = 'scheduled' and timer.due_at <= v_snapshot_at then 'due'
                when timer.status = 'scheduled' then 'timer_scheduled'
                when timer.status = 'completed' then 'reevaluated'
                when correlation.has_correlation then 'correlated'
                else 'intent_created' end,
           case when intent.lifecycle_state = 'purchased' then 'purchased'
                else 'unknown' end,
           array_remove(array[
               case when command.status in ('delivery_unknown', 'failed', 'blocked')
                    then command.status end,
               case when handoff.status is not null then 'handoff_' || handoff.status end,
               case when opt_out.has_opt_out then 'opt_out' end
           ]::text[], null),
           intent.created_at,
           least(intent.updated_at, v_snapshot_at),
           null::uuid,
           command.chatwoot_conversation_id,
           null::text,
           array_remove(array[
               case when not test_command.is_controlled_test and not correlation.only_simulator
                    then 'provenance_unknown' end,
               case when correlation.problem_outcome is not null
                    then 'correlation_' || correlation.problem_outcome end,
               case when command.status = 'delivery_unknown' then 'delivery_unknown' end,
               case when handoff.status is not null
                          and handoff.status not in ('projected', 'completed')
                    then 'handoff_pending' end
           ]::text[], null),
           funnel.page_view_count,
           funnel.preform_opened_count,
           funnel.preform_submitted_count,
           funnel.checkout_redirected_count,
           funnel.last_funnel_event_at,
           funnel.last_funnel_event_type
    from public.purchase_intents intent
    left join lateral (
        select count(*) > 0 as has_submission
        from public.purchase_intent_submissions link
        join public.precheckout_submissions submission
          on link.submission_id = submission.id
         and submission.received_at < v_snapshot_at
        where link.purchase_intent_id = intent.id
          and link.attached_at < v_snapshot_at
    ) submission on true
    left join lateral (
        select count(*) > 0 as has_correlation,
               coalesce(bool_and(event.source = 'simulator'), false) as only_simulator,
               min(correlation_row.outcome) filter (
                   where correlation_row.outcome in ('unmatched', 'ambiguous', 'conflict')
               ) as problem_outcome
        from public.hotmart_purchase_intent_correlations correlation_row
        join public.webhook_events event
          on event.id = correlation_row.webhook_event_id
         and event.received_at < v_snapshot_at
        where correlation_row.purchase_intent_id = intent.id
          and correlation_row.created_at < v_snapshot_at
    ) correlation on true
    left join lateral (
        select timer_row.status, timer_row.due_at
        from public.hotmart_abandonment_reevaluations timer_row
        where timer_row.purchase_intent_id = intent.id
          and timer_row.created_at < v_snapshot_at
        order by timer_row.created_at desc, timer_row.id desc limit 1
    ) timer on true
    left join lateral (
        select command_row.status, command_row.chatwoot_conversation_id
        from public.johanna_abandonment_one_shot_commands command_row
        where command_row.purchase_intent_id = intent.id
          and command_row.chatwoot_account_id = 1
          and command_row.chatwoot_inbox_id = 9
          and command_row.created_at < v_snapshot_at
        order by command_row.created_at desc, command_row.id desc limit 1
    ) command on true
    left join lateral (
        select handoff_row.status
        from public.human_handoff_requests handoff_row
        where handoff_row.external_conversation_id = command.chatwoot_conversation_id
          and handoff_row.chatwoot_account_id = 1
          and handoff_row.chatwoot_inbox_id = 9
          and handoff_row.created_at < v_snapshot_at
        order by handoff_row.created_at desc, handoff_row.id desc limit 1
    ) handoff on true
    left join lateral (
        select exists (
            select 1 from public.contact_opt_out_events opt_out_row
            where opt_out_row.canonical_conversation_id = command.chatwoot_conversation_id
              and opt_out_row.canonical_account_id = 1
              and opt_out_row.canonical_inbox_id = 9
              and opt_out_row.correlation_status = 'applied'
              and opt_out_row.occurred_at >= intent.created_at
              and opt_out_row.occurred_at < v_snapshot_at
        ) as has_opt_out
    ) opt_out on true
    left join lateral (
        select exists (
            select 1 from public.precheckout_test_first_touch_commands test_row
            where test_row.purchase_intent_id = intent.id
              and test_row.test_only and test_row.created_at < v_snapshot_at
        ) as is_controlled_test
    ) test_command on true
    left join lateral (
        select count(*) filter (where event.event_type = 'page_view')::bigint,
               count(*) filter (where event.event_type = 'preform_opened')::bigint,
               count(*) filter (where event.event_type = 'preform_submitted')::bigint,
               count(*) filter (where event.event_type = 'checkout_redirected')::bigint,
               max(event.occurred_at),
               (array_agg(event.event_type order by event.occurred_at desc, event.event_id desc))[1]
        from public.purchase_intent_submissions link
        join public.precheckout_submissions submission
          on link.submission_id = submission.id
         and submission.received_at < v_snapshot_at
        join public.johanna_funnel_events event
          on submission.external_submission_id = event.anonymous_session_id
         and event.landing_ref = intent.landing_ref
         and event.offer_ref = intent.offer_ref
         and event.occurred_at >= v_snapshot_at - make_interval(days => p_window_days)
         and event.occurred_at < v_snapshot_at
        where link.purchase_intent_id = intent.id
          and link.attached_at < v_snapshot_at
    ) funnel(page_view_count, preform_opened_count, preform_submitted_count,
             checkout_redirected_count, last_funnel_event_at,
             last_funnel_event_type) on true
    where intent.tenant_ref = 'lancemos'
      and intent.funnel_ref = 'psicologajohanna'
      and lower(intent.product_ref) = 'f106691755g'
      and public.is_johanna_precheckout_pair(intent.landing_ref, intent.offer_ref)
      and intent.created_at >= v_snapshot_at - make_interval(days => p_window_days)
      and intent.created_at < v_snapshot_at

    union all

    select commercial_case.id, 'inbound'::text, 'unknown'::text,
           commercial_case.status, 'unknown'::text,
           array_remove(array[
               case when handoff.status is not null then 'handoff_' || handoff.status end,
               case when opt_out.has_opt_out then 'opt_out' end
           ]::text[], null),
           commercial_case.created_at, least(commercial_case.updated_at, v_snapshot_at),
           commercial_case.conversation_id, admission.external_conversation_id,
           null::text,
           array_remove(array[
               'provenance_unknown',
               case when commercial_case.identity_resolution_status in
                    ('ambiguous', 'conflict', 'unmatched')
                    then 'identity_' || commercial_case.identity_resolution_status end,
               case when handoff.status is not null
                          and handoff.status not in ('projected', 'completed')
                    then 'handoff_pending' end
           ]::text[], null),
           0::bigint, 0::bigint, 0::bigint, 0::bigint, null::timestamptz, null::text
    from public.commercial_cases commercial_case
    join public.inbound_commercial_scope_versions inbound_scope
      on inbound_scope.scope_key = commercial_case.inbound_scope_key
     and inbound_scope.version = commercial_case.inbound_scope_version
     and inbound_scope.tenant_key = 'lancemos'
     and lower(inbound_scope.external_product_id) = 'f106691755g'
     and inbound_scope.chatwoot_account_id = 1
     and inbound_scope.chatwoot_inbox_id = 9
     and inbound_scope.created_at < v_snapshot_at
    left join public.inbound_commercial_case_admissions admission
      on admission.commercial_case_id = commercial_case.id
     and admission.created_at < v_snapshot_at
    left join lateral (
        select handoff_row.status from public.human_handoff_requests handoff_row
        where handoff_row.commercial_case_id = commercial_case.id
          and handoff_row.chatwoot_account_id = 1
          and handoff_row.chatwoot_inbox_id = 9
          and handoff_row.created_at < v_snapshot_at
        order by handoff_row.created_at desc, handoff_row.id desc limit 1
    ) handoff on true
    left join lateral (
        select exists (
            select 1 from public.contact_opt_out_events opt_out_row
            where opt_out_row.canonical_conversation_id = admission.external_conversation_id
              and opt_out_row.canonical_account_id = 1
              and opt_out_row.canonical_inbox_id = 9
              and opt_out_row.correlation_status = 'applied'
              and opt_out_row.occurred_at >= commercial_case.created_at
              and opt_out_row.occurred_at < v_snapshot_at
        ) as has_opt_out
    ) opt_out on true
    where commercial_case.case_kind = 'inbound_sales'
      and commercial_case.tenant_ref = 'lancemos'
      and commercial_case.inbound_scope_key = 'libre-de-ansiedad-inbound'
      and commercial_case.inbound_scope_version = 2
      and lower(commercial_case.product_ref) = 'f106691755g'
      and commercial_case.created_at >= v_snapshot_at - make_interval(days => p_window_days)
      and commercial_case.created_at < v_snapshot_at

    union all

    select payment.id, 'payment_failure'::text, 'unknown'::text,
           coalesce(command.status, payment.case_status),
           case when intent.lifecycle_state = 'purchased' then 'purchased' else 'unknown' end,
           array_remove(array[
               case when command.status in ('delivery_unknown', 'failed', 'blocked')
                    then command.status end,
               case when handoff.status is not null then 'handoff_' || handoff.status end,
               case when opt_out.has_opt_out then 'opt_out' end
           ]::text[], null),
           payment.created_at, least(greatest(payment.observed_at, payment.created_at), v_snapshot_at),
           null::uuid, command.chatwoot_conversation_id, null::text,
           array_remove(array[
               'provenance_unknown',
               case when payment.correlation_outcome in ('unmatched', 'ambiguous', 'conflict')
                    then 'correlation_' || payment.correlation_outcome end,
               case when command.status = 'delivery_unknown' then 'delivery_unknown' end,
               case when handoff.status is not null
                          and handoff.status not in ('projected', 'completed')
                    then 'handoff_pending' end
           ]::text[], null),
           0::bigint, 0::bigint, 0::bigint, 0::bigint, null::timestamptz, null::text
    from public.johanna_payment_failure_cases payment
    join public.purchase_intents intent
      on intent.id = payment.purchase_intent_id
     and intent.tenant_ref = 'lancemos'
     and intent.funnel_ref = 'psicologajohanna'
     and lower(intent.product_ref) = 'f106691755g'
     and public.is_johanna_precheckout_pair(intent.landing_ref, intent.offer_ref)
     and intent.created_at < v_snapshot_at
    left join lateral (
        select command_row.status, command_row.chatwoot_conversation_id
        from public.johanna_abandonment_one_shot_commands command_row
        where command_row.payment_failure_case_id = payment.id
          and command_row.chatwoot_account_id = 1
          and command_row.chatwoot_inbox_id = 9
          and command_row.created_at < v_snapshot_at
        order by command_row.created_at desc, command_row.id desc limit 1
    ) command on true
    left join lateral (
        select handoff_row.status from public.human_handoff_requests handoff_row
        where handoff_row.external_conversation_id = command.chatwoot_conversation_id
          and handoff_row.chatwoot_account_id = 1
          and handoff_row.chatwoot_inbox_id = 9
          and handoff_row.created_at < v_snapshot_at
        order by handoff_row.created_at desc, handoff_row.id desc limit 1
    ) handoff on true
    left join lateral (
        select exists (
            select 1 from public.contact_opt_out_events opt_out_row
            where opt_out_row.canonical_conversation_id = command.chatwoot_conversation_id
              and opt_out_row.canonical_account_id = 1
              and opt_out_row.canonical_inbox_id = 9
              and opt_out_row.correlation_status = 'applied'
              and opt_out_row.occurred_at >= payment.created_at
              and opt_out_row.occurred_at < v_snapshot_at
        ) as has_opt_out
    ) opt_out on true
    where payment.created_at >= v_snapshot_at - make_interval(days => p_window_days)
      and payment.created_at < v_snapshot_at
    )
    select 'meta'::text,
           v_snapshot_at,
           v_snapshot_at - make_interval(days => p_window_days),
           null::uuid, null::text, null::text, null::text, null::text,
           null::text[], null::timestamptz, null::timestamptz, null::uuid,
           null::bigint, null::text, null::text[],
           null::bigint, null::bigint, null::bigint, null::bigint,
           null::timestamptz, null::text
    union all
    select 'case'::text,
           v_snapshot_at,
           v_snapshot_at - make_interval(days => p_window_days),
           dashboard_case.*
    from dashboard_cases dashboard_case
    order by 1 desc, 10 desc nulls last, 4;
end;
$function$;

revoke all on table public.johanna_funnel_events from public;
revoke all on function public.johanna_funnel_events_append_only() from public;
revoke all on function public.admit_johanna_funnel_event_v1(text,text,text,timestamptz,text,text,text,text,text,text,text,text) from public;
revoke all on function public.read_johanna_funnel_dashboard_v2(integer) from public;
revoke all on function public.read_johanna_funnel_dashboard_v1(timestamptz,integer) from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.johanna_funnel_events from anon;
        revoke all on function public.johanna_funnel_events_append_only() from anon;
        revoke all on function public.admit_johanna_funnel_event_v1(text,text,text,timestamptz,text,text,text,text,text,text,text,text) from anon;
        revoke all on function public.read_johanna_funnel_dashboard_v2(integer) from anon;
        revoke all on function public.read_johanna_funnel_dashboard_v1(timestamptz,integer) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.johanna_funnel_events from authenticated;
        revoke all on function public.johanna_funnel_events_append_only() from authenticated;
        revoke all on function public.admit_johanna_funnel_event_v1(text,text,text,timestamptz,text,text,text,text,text,text,text,text) from authenticated;
        revoke all on function public.read_johanna_funnel_dashboard_v2(integer) from authenticated;
        revoke all on function public.read_johanna_funnel_dashboard_v1(timestamptz,integer) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.johanna_funnel_events from service_role;
        revoke all on function public.johanna_funnel_events_append_only() from service_role;
        grant execute on function public.admit_johanna_funnel_event_v1(text,text,text,timestamptz,text,text,text,text,text,text,text,text) to service_role;
        grant execute on function public.read_johanna_funnel_dashboard_v2(integer) to service_role;
        revoke all on function public.read_johanna_funnel_dashboard_v1(timestamptz,integer) from service_role;
    end if;
end;
$acl$;

commit;
