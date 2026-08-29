-- Generalize the existing Hotmart reevaluation timer to admit a durable,
-- default-off precheckout source. This migration creates no outbound command or
-- sender effect; precheckout timers are deliberately excluded from the due list.

begin;

alter table public.hotmart_abandonment_timer_policy_bindings
    add column precheckout_first_touch_enabled boolean not null default false;

alter table public.hotmart_abandonment_timer_policy_binding_events
    add column precheckout_first_touch_enabled boolean not null default false;

alter table public.hotmart_abandonment_reevaluations
    add column source_kind text not null default 'hotmart_event',
    add column source_submission_id uuid
        references public.precheckout_submissions(id) on delete restrict;

alter table public.hotmart_abandonment_reevaluations
    alter column source_webhook_event_id drop not null,
    alter column source_scope_id drop not null,
    add constraint hotmart_abandonment_reevaluations_source_kind_check
        check (source_kind in ('hotmart_event', 'precheckout_intent')),
    add constraint hotmart_abandonment_reevaluations_source_shape_check
        check (
            (
                source_kind = 'hotmart_event'
                and source_webhook_event_id is not null
                and source_scope_id is not null
                and source_submission_id is null
            ) or (
                source_kind = 'precheckout_intent'
                and source_webhook_event_id is null
                and source_scope_id is null
                and source_submission_id is not null
            )
        ),
    add constraint hotmart_abandonment_reevaluations_submission_key
        unique (source_submission_id);

create or replace function public.record_hotmart_abandonment_timer_policy_binding()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_delay_seconds integer;
begin
    select extract(epoch from policy.grace_period)::integer
    into strict v_delay_seconds
    from public.followup_policy_versions policy
    where policy.policy_key = new.policy_key
      and policy.version = new.policy_version;

    insert into public.hotmart_abandonment_timer_policy_binding_events (
        binding_id,
        generation,
        tenant_ref,
        funnel_ref,
        product_ref,
        offer_ref,
        enabled,
        precheckout_first_touch_enabled,
        policy_key,
        policy_version,
        delay_seconds
    ) values (
        new.id,
        new.generation,
        new.tenant_ref,
        new.funnel_ref,
        new.product_ref,
        new.offer_ref,
        new.enabled,
        new.precheckout_first_touch_enabled,
        new.policy_key,
        new.policy_version,
        v_delay_seconds
    );
    return new;
end;
$function$;

create or replace function public.protect_hotmart_abandonment_reevaluation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if old.source_kind = 'precheckout_intent'
       and old.status = 'scheduled'
       and new.status = 'scheduled'
       and new.source_submission_id is distinct from old.source_submission_id
       and new.observed_at > old.observed_at
       and new.due_at = new.observed_at
           + make_interval(secs => new.delay_seconds_snapshot)
       and new.idempotency_key =
           'precheckout-first-touch:' || new.source_submission_id::text
       and new.id is not distinct from old.id
       and new.purchase_intent_id is not distinct from old.purchase_intent_id
       and new.source_kind is not distinct from old.source_kind
       and new.source_webhook_event_id is not distinct from old.source_webhook_event_id
       and new.source_scope_id is not distinct from old.source_scope_id
       and new.policy_binding_id is not distinct from old.policy_binding_id
       and new.policy_binding_generation is not distinct from old.policy_binding_generation
       and new.policy_key is not distinct from old.policy_key
       and new.policy_version is not distinct from old.policy_version
       and new.delay_seconds_snapshot is not distinct from old.delay_seconds_snapshot
       and new.created_at is not distinct from old.created_at
       and new.outcome is not distinct from old.outcome
       and new.completed_at is not distinct from old.completed_at then
        return new;
    end if;

    if new.id is distinct from old.id
       or new.purchase_intent_id is distinct from old.purchase_intent_id
       or new.source_kind is distinct from old.source_kind
       or new.source_submission_id is distinct from old.source_submission_id
       or new.source_webhook_event_id is distinct from old.source_webhook_event_id
       or new.source_scope_id is distinct from old.source_scope_id
       or new.policy_binding_id is distinct from old.policy_binding_id
       or new.policy_binding_generation is distinct from old.policy_binding_generation
       or new.policy_key is distinct from old.policy_key
       or new.policy_version is distinct from old.policy_version
       or new.delay_seconds_snapshot is distinct from old.delay_seconds_snapshot
       or new.observed_at is distinct from old.observed_at
       or new.due_at is distinct from old.due_at
       or new.idempotency_key is distinct from old.idempotency_key
       or new.created_at is distinct from old.created_at then
        raise exception using
            errcode = '55000',
            message = 'hotmart_abandonment_reevaluation_identity_immutable';
    end if;

    if old.status = 'scheduled'
       and new.status = 'completed'
       and new.outcome is not null
       and new.completed_at is not null then
        return new;
    end if;
    if old.status = 'completed'
       and old.outcome is distinct from 'cancelled_purchased'
       and new.status = 'completed'
       and new.outcome = 'cancelled_purchased'
       and new.completed_at is not null then
        return new;
    end if;
    if new.status is not distinct from old.status
       and new.outcome is not distinct from old.outcome
       and new.completed_at is not distinct from old.completed_at then
        return new;
    end if;

    raise exception using
        errcode = '55000',
        message = 'hotmart_abandonment_reevaluation_transition_invalid';
end;
$function$;

create or replace function public.schedule_precheckout_first_touch_reevaluation(
    p_purchase_intent_id uuid,
    p_submission_id uuid
)
returns table (
    outcome text,
    reevaluation_id uuid,
    created boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_binding public.hotmart_abandonment_timer_policy_bindings%rowtype;
    v_policy public.followup_policy_versions%rowtype;
    v_existing public.hotmart_abandonment_reevaluations%rowtype;
    v_delay_seconds integer;
    v_observed_at timestamptz;
    v_reevaluation_id uuid;
begin
    if p_purchase_intent_id is null or p_submission_id is null then
        raise exception using
            errcode = '22023',
            message = 'precheckout_first_touch_timer_input_invalid';
    end if;

    -- Preserve the timer subsystem's global lock order: intent before source.
    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id
    for update;
    if not found then
        raise exception using
            errcode = 'P0002',
            message = 'precheckout_first_touch_intent_not_found';
    end if;

    select submission.* into v_submission
    from public.precheckout_submissions submission
    join public.purchase_intent_submissions link
      on link.submission_id = submission.id
     and link.purchase_intent_id = v_intent.id
    where submission.id = p_submission_id
    for share of submission;
    if not found then
        raise exception using
            errcode = '23514',
            message = 'precheckout_first_touch_submission_mismatch';
    end if;

    if v_submission.contract_version is distinct from '1.1.0'
       or v_submission.provisional
       or not v_submission.provider_observed
       or not v_submission.activation_authorized
       or v_submission.canonical_payload #>> '{consent,marketing_optin}' is distinct from 'true'
       or v_submission.canonical_payload #>> '{consent,whatsapp_contact}' is distinct from 'true'
       or v_submission.canonical_payload #>> '{consent,copy_version}'
          is distinct from 'johanna-precheckout-whatsapp-disclosure-v1'
       or v_intent.tenant_ref is distinct from 'lancemos'
       or v_intent.funnel_ref is distinct from 'psicologajohanna'
       or v_intent.landing_ref is distinct from 'ads-a'
       or lower(v_intent.product_ref) is distinct from lower('F106691755G')
       or v_intent.offer_ref is distinct from 'bxjge6zq'
       or v_intent.lifecycle_state is distinct from 'waiting_for_purchase'
       or v_intent.current_classification is not null
       or v_intent.current_classification = 'identity_conflict'
       or v_intent.provisional
       or not v_intent.provider_observed
       or not v_intent.activation_authorized
       or not v_intent.whatsapp_contact_authorized
       or v_intent.normalized_phone is null
       or v_submission.canonical_payload #>> '{identity,email}'
          is distinct from v_intent.normalized_email
       or v_submission.canonical_payload #>> '{identity,phone}'
          is distinct from v_intent.normalized_phone then
        return query select 'not_eligible'::text, null::uuid, false;
        return;
    end if;

    begin
        v_observed_at := (
            v_submission.canonical_payload #>> '{submitted_at}'
        )::timestamptz;
    exception when others then
        raise exception using
            errcode = '23514',
            message = 'precheckout_first_touch_submitted_at_invalid';
    end;
    if v_observed_at is null then
        raise exception using
            errcode = '23514',
            message = 'precheckout_first_touch_submitted_at_invalid';
    end if;

    select reevaluation.* into v_existing
    from public.hotmart_abandonment_reevaluations reevaluation
    where reevaluation.source_submission_id = v_submission.id;
    if found then
        return query select
            case
                when v_existing.status = 'scheduled' then 'scheduled'
                else v_existing.outcome
            end,
            v_existing.id,
            false;
        return;
    end if;

    select binding.* into v_binding
    from public.hotmart_abandonment_timer_policy_bindings binding
    where binding.tenant_ref = v_intent.tenant_ref
      and binding.funnel_ref = v_intent.funnel_ref
      and (
          binding.product_ref is null
          or lower(binding.product_ref) = lower(v_intent.product_ref)
      )
      and (
          binding.offer_ref is null
          or binding.offer_ref = v_intent.offer_ref
      )
    order by case
                 when binding.product_ref is not null
                  and binding.offer_ref is not null then 2
                 when binding.product_ref is not null then 1
                 else 0
             end desc,
             binding.id
    limit 1
    for share of binding;

    if not found
       or not v_binding.enabled
       or not v_binding.precheckout_first_touch_enabled then
        return query select 'scheduling_disabled'::text, null::uuid, false;
        return;
    end if;

    select policy.* into strict v_policy
    from public.followup_policy_versions policy
    where policy.policy_key = v_binding.policy_key
      and policy.version = v_binding.policy_version
      and policy.status = 'published'
      and policy.purpose = 'cart_recovery';
    v_delay_seconds := extract(epoch from v_policy.grace_period)::integer;
    if v_delay_seconds <> 3600 then
        raise exception using
            errcode = '23514',
            message = 'precheckout_first_touch_delay_must_be_60_minutes';
    end if;

    perform 1
    from public.hotmart_abandonment_timer_policy_binding_events event
    where event.binding_id = v_binding.id
      and event.generation = v_binding.generation
      and event.enabled
      and event.precheckout_first_touch_enabled
      and event.policy_key = v_binding.policy_key
      and event.policy_version = v_binding.policy_version
      and event.delay_seconds = v_delay_seconds;
    if not found then
        raise exception using
            errcode = '23514',
            message = 'precheckout_first_touch_policy_snapshot_missing';
    end if;

    select reevaluation.* into v_existing
    from public.hotmart_abandonment_reevaluations reevaluation
    where reevaluation.purchase_intent_id = v_intent.id
      and reevaluation.status = 'scheduled'
    for update;
    if found then
        if v_observed_at > v_existing.observed_at then
            update public.hotmart_abandonment_reevaluations
            set source_submission_id = v_submission.id,
                observed_at = v_observed_at,
                due_at = v_observed_at + make_interval(secs => v_delay_seconds),
                idempotency_key = 'precheckout-first-touch:' || v_submission.id::text,
                updated_at = clock_timestamp()
            where id = v_existing.id;
        end if;
        return query select 'coalesced_existing_timer'::text, v_existing.id, false;
        return;
    end if;

    insert into public.hotmart_abandonment_reevaluations (
        purchase_intent_id,
        source_kind,
        source_submission_id,
        source_webhook_event_id,
        source_scope_id,
        policy_binding_id,
        policy_binding_generation,
        policy_key,
        policy_version,
        delay_seconds_snapshot,
        observed_at,
        due_at,
        idempotency_key
    ) values (
        v_intent.id,
        'precheckout_intent',
        v_submission.id,
        null,
        null,
        v_binding.id,
        v_binding.generation,
        v_binding.policy_key,
        v_binding.policy_version,
        v_delay_seconds,
        v_observed_at,
        v_observed_at + make_interval(secs => v_delay_seconds),
        'precheckout-first-touch:' || v_submission.id::text
    )
    on conflict (source_submission_id) do nothing
    returning id into v_reevaluation_id;

    if v_reevaluation_id is null then
        select reevaluation.* into strict v_existing
        from public.hotmart_abandonment_reevaluations reevaluation
        where reevaluation.source_submission_id = v_submission.id;
        return query select 'scheduled'::text, v_existing.id, false;
        return;
    end if;

    return query select 'scheduled'::text, v_reevaluation_id, true;
end;
$function$;

-- Keep precheckout timers inert until the separately authorized effect task.
create or replace function public.list_due_hotmart_abandonment_reevaluations(
    p_now timestamptz,
    p_batch_size integer
)
returns table (
    reevaluation_id uuid
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_now is null
       or p_batch_size is null
       or not (p_batch_size between 1 and 100) then
        raise exception using
            errcode = '22023',
            message = 'hotmart_abandonment_due_list_input_invalid';
    end if;

    return query
    select reevaluation.id
    from public.hotmart_abandonment_reevaluations reevaluation
    where reevaluation.status = 'scheduled'
      and reevaluation.source_kind = 'hotmart_event'
      and reevaluation.due_at <= p_now
    order by reevaluation.due_at, reevaluation.id
    limit p_batch_size;
end;
$function$;

do $guard_precheckout_reevaluation$
declare
    v_function regprocedure :=
        to_regprocedure('public.reevaluate_hotmart_abandonment_timer(uuid,timestamptz)');
    v_definition text;
    v_old text := $old$    if v_reevaluation.status = 'completed' then$old$;
    v_new text := $new$    if v_reevaluation.source_kind = 'precheckout_intent' then
        raise exception using
            errcode = '55000',
            message = 'precheckout_first_touch_not_active';
    end if;

    if v_reevaluation.status = 'completed' then$new$;
    v_occurrences integer;
begin
    if v_function is null then
        raise exception 'hotmart_abandonment_reevaluation_function_missing';
    end if;
    select pg_get_functiondef(v_function) into strict v_definition;
    v_occurrences := (
        length(v_definition) - length(replace(v_definition, v_old, ''))
    ) / length(v_old);
    if v_occurrences <> 1 then
        raise exception 'precheckout_reevaluation_guard_marker_mismatch';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$guard_precheckout_reevaluation$;

-- Install the scheduling call inside the existing admission transaction without
-- copying or weakening its validation surface.
do $hook_observed_precheckout_timer$
declare
    v_function regprocedure :=
        to_regprocedure('public.admit_observed_lead_precheckout(text,jsonb,jsonb)');
    v_definition text;
    v_old text := $old$    insert into public.purchase_intent_submissions (
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

    return query select 'inserted'::text, v_submission_id, v_purchase_intent_id;$old$;
    v_new text := $new$    insert into public.purchase_intent_submissions (
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

    if v_contract_version = '1.1.0' then
        perform public.schedule_precheckout_first_touch_reevaluation(
            v_purchase_intent_id,
            v_submission_id
        );
    end if;

    return query select 'inserted'::text, v_submission_id, v_purchase_intent_id;$new$;
    v_occurrences integer;
begin
    if v_function is null then
        raise exception 'observed_precheckout_admission_function_missing';
    end if;
    select pg_get_functiondef(v_function) into strict v_definition;
    v_occurrences := (
        length(v_definition) - length(replace(v_definition, v_old, ''))
    ) / length(v_old);
    if v_occurrences <> 1 then
        raise exception 'observed_precheckout_timer_hook_marker_mismatch';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$hook_observed_precheckout_timer$;

revoke all on function public.schedule_precheckout_first_touch_reevaluation(uuid, uuid)
from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.schedule_precheckout_first_touch_reevaluation(uuid, uuid)
        from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.schedule_precheckout_first_touch_reevaluation(uuid, uuid)
        from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.schedule_precheckout_first_touch_reevaluation(uuid, uuid)
        to service_role;
    end if;
end;
$acl$;

do $postflight$
declare
    v_admission text;
    v_due_list text;
    v_reevaluate text;
begin
    select pg_get_functiondef(
        to_regprocedure('public.admit_observed_lead_precheckout(text,jsonb,jsonb)')
    ) into strict v_admission;
    select pg_get_functiondef(
        to_regprocedure('public.list_due_hotmart_abandonment_reevaluations(timestamptz,integer)')
    ) into strict v_due_list;
    select pg_get_functiondef(
        to_regprocedure('public.reevaluate_hotmart_abandonment_timer(uuid,timestamptz)')
    ) into strict v_reevaluate;

    if position(
        'perform public.schedule_precheckout_first_touch_reevaluation('
        in lower(v_admission)
    ) = 0
       or position($marker$reevaluation.source_kind = 'hotmart_event'$marker$ in lower(v_due_list)) = 0
       or position('precheckout_first_touch_not_active' in lower(v_reevaluate)) = 0 then
        raise exception 'precheckout_delayed_first_touch_timer_postflight_failed';
    end if;
end;
$postflight$;

commit;
