-- Configurable, durable reevaluation timer for resolved Hotmart abandonment.
-- This migration does not create commercial scheduled_actions or outbound effects.

begin;

create table public.hotmart_abandonment_timer_policy_bindings (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null,
    funnel_ref text not null,
    product_ref text,
    offer_ref text,
    enabled boolean not null default false,
    policy_key text not null,
    policy_version integer not null,
    generation bigint not null default 1 check (generation > 0),
    activated_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    foreign key (policy_key, policy_version)
        references public.followup_policy_versions(policy_key, version)
        on delete restrict,
    check (nullif(btrim(tenant_ref), '') is not null),
    check (nullif(btrim(funnel_ref), '') is not null),
    check (product_ref is null or nullif(btrim(product_ref), '') is not null),
    check (offer_ref is null or nullif(btrim(offer_ref), '') is not null),
    check (offer_ref is null or product_ref is not null)
);

create unique index hotmart_abandonment_timer_policy_bindings_scope_idx
on public.hotmart_abandonment_timer_policy_bindings (
    tenant_ref,
    funnel_ref,
    coalesce(lower(product_ref), ''),
    coalesce(offer_ref, '')
);

create table public.hotmart_abandonment_timer_policy_binding_events (
    binding_id uuid not null
        references public.hotmart_abandonment_timer_policy_bindings(id)
        on delete restrict,
    generation bigint not null check (generation > 0),
    tenant_ref text not null,
    funnel_ref text not null,
    product_ref text,
    offer_ref text,
    enabled boolean not null,
    policy_key text not null,
    policy_version integer not null,
    delay_seconds integer not null check (delay_seconds between 60 and 2592000),
    recorded_at timestamptz not null default clock_timestamp(),
    primary key (binding_id, generation),
    unique (
        binding_id,
        generation,
        policy_key,
        policy_version,
        delay_seconds
    ),
    foreign key (policy_key, policy_version)
        references public.followup_policy_versions(policy_key, version)
        on delete restrict,
    check (offer_ref is null or product_ref is not null)
);

create or replace function public.validate_hotmart_abandonment_timer_policy_binding()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_policy public.followup_policy_versions%rowtype;
    v_delay_seconds numeric;
begin
    if tg_op = 'DELETE' then
        raise exception using
            errcode = '55000',
            message = 'hotmart_abandonment_timer_policy_binding_delete_forbidden';
    end if;

    if tg_op = 'INSERT' then
        if new.generation <> 1 then
            raise exception using
                errcode = '22023',
                message = 'hotmart_abandonment_timer_policy_binding_initial_generation_invalid';
        end if;
    else
        if new.id is distinct from old.id
           or new.tenant_ref is distinct from old.tenant_ref
           or new.funnel_ref is distinct from old.funnel_ref
           or new.product_ref is distinct from old.product_ref
           or new.offer_ref is distinct from old.offer_ref
           or new.activated_at is distinct from old.activated_at then
            raise exception using
                errcode = '55000',
                message = 'hotmart_abandonment_timer_policy_binding_scope_immutable';
        end if;
        if new.generation <> old.generation + 1 then
            raise exception using
                errcode = '22023',
                message = 'hotmart_abandonment_timer_policy_binding_generation_invalid';
        end if;
        new.updated_at := clock_timestamp();
    end if;

    select policy.* into v_policy
    from public.followup_policy_versions policy
    where policy.policy_key = new.policy_key
      and policy.version = new.policy_version
      and policy.status = 'published'
      and policy.purpose = 'cart_recovery';

    if not found then
        raise exception using
            errcode = '23514',
            message = 'hotmart_abandonment_timer_policy_not_published';
    end if;

    v_delay_seconds := extract(epoch from v_policy.grace_period);
    if v_delay_seconds < 60
       or v_delay_seconds > 2592000
       or trunc(v_delay_seconds) <> v_delay_seconds
       or v_policy.grace_period >= v_policy.expires_after then
        raise exception using
            errcode = '23514',
            message = 'hotmart_abandonment_timer_policy_delay_invalid';
    end if;

    return new;
end;
$function$;

create trigger hotmart_abandonment_timer_policy_bindings_validate
before insert or update or delete
on public.hotmart_abandonment_timer_policy_bindings
for each row execute function public.validate_hotmart_abandonment_timer_policy_binding();

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
        new.policy_key,
        new.policy_version,
        v_delay_seconds
    );
    return new;
end;
$function$;

create trigger hotmart_abandonment_timer_policy_bindings_record
    after insert or update
    on public.hotmart_abandonment_timer_policy_bindings
    for each row execute function public.record_hotmart_abandonment_timer_policy_binding();

create or replace function public.protect_hotmart_abandonment_timer_policy_binding_event()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using
        errcode = '55000',
        message = 'hotmart_abandonment_timer_policy_binding_event_immutable';
end;
$function$;

create trigger hotmart_abandonment_timer_policy_binding_events_immutable
before update or delete on public.hotmart_abandonment_timer_policy_binding_events
for each row execute function public.protect_hotmart_abandonment_timer_policy_binding_event();

create table public.hotmart_abandonment_reevaluations (
    id uuid primary key default gen_random_uuid(),
    purchase_intent_id uuid not null
        references public.purchase_intents(id) on delete restrict,
    source_webhook_event_id uuid not null unique
        references public.hotmart_purchase_intent_correlations(webhook_event_id)
        on delete restrict,
    source_scope_id uuid not null
        references public.hotmart_purchase_intent_scopes(id) on delete restrict,
    policy_binding_id uuid not null,
    policy_binding_generation bigint not null check (policy_binding_generation > 0),
    policy_key text not null,
    policy_version integer not null,
    delay_seconds_snapshot integer not null
        check (delay_seconds_snapshot between 60 and 2592000),
    observed_at timestamptz not null,
    due_at timestamptz not null,
    status text not null default 'scheduled'
        check (status in ('scheduled', 'completed')),
    outcome text check (outcome in (
        'cancelled_purchased',
        'blocked_not_authorized',
        'blocked_contact_binding_missing',
        'cancelled_intent_changed'
    )),
    idempotency_key text not null unique,
    completed_at timestamptz,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    foreign key (
        policy_binding_id,
        policy_binding_generation,
        policy_key,
        policy_version,
        delay_seconds_snapshot
    ) references public.hotmart_abandonment_timer_policy_binding_events (
        binding_id,
        generation,
        policy_key,
        policy_version,
        delay_seconds
    ) on delete restrict,
    check (nullif(btrim(idempotency_key), '') is not null),
    check (
        due_at = observed_at + make_interval(secs => delay_seconds_snapshot)
    ),
    check (
        (status = 'scheduled' and outcome is null and completed_at is null)
        or (status = 'completed' and outcome is not null and completed_at is not null)
    )
);

create unique index hotmart_abandonment_reevaluations_one_scheduled_per_intent_idx
on public.hotmart_abandonment_reevaluations (purchase_intent_id)
where status = 'scheduled';

create index hotmart_abandonment_reevaluations_due_idx
on public.hotmart_abandonment_reevaluations (due_at, id)
where status = 'scheduled';

create table public.hotmart_abandonment_reevaluation_events (
    id bigint generated always as identity primary key,
    reevaluation_id uuid not null
        references public.hotmart_abandonment_reevaluations(id) on delete restrict,
    from_status text,
    to_status text not null,
    from_outcome text,
    to_outcome text,
    reason_code text not null,
    occurred_at timestamptz not null default clock_timestamp(),
    check (nullif(btrim(reason_code), '') is not null)
);

create or replace function public.protect_hotmart_abandonment_reevaluation_event()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using
        errcode = '55000',
        message = 'hotmart_abandonment_reevaluation_event_append_only';
end;
$function$;

create trigger hotmart_abandonment_reevaluation_events_append_only
before update or delete on public.hotmart_abandonment_reevaluation_events
for each row execute function public.protect_hotmart_abandonment_reevaluation_event();

create or replace function public.protect_hotmart_abandonment_reevaluation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if new.id is distinct from old.id
       or new.purchase_intent_id is distinct from old.purchase_intent_id
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

create trigger hotmart_abandonment_reevaluations_protect
before update on public.hotmart_abandonment_reevaluations
for each row execute function public.protect_hotmart_abandonment_reevaluation();

create or replace function public.record_hotmart_abandonment_reevaluation_event()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'INSERT'
       or new.status is distinct from old.status
       or new.outcome is distinct from old.outcome then
        insert into public.hotmart_abandonment_reevaluation_events (
            reevaluation_id,
            from_status,
            to_status,
            from_outcome,
            to_outcome,
            reason_code
        ) values (
            new.id,
            case when tg_op = 'INSERT' then null else old.status end,
            new.status,
            case when tg_op = 'INSERT' then null else old.outcome end,
            new.outcome,
            coalesce(new.outcome, 'scheduled')
        );
    end if;
    return new;
end;
$function$;

create trigger hotmart_abandonment_reevaluations_record
after insert or update on public.hotmart_abandonment_reevaluations
for each row execute function public.record_hotmart_abandonment_reevaluation_event();

create or replace function public.schedule_hotmart_abandonment_reevaluation(
    p_webhook_event_id uuid
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
    v_correlation public.hotmart_purchase_intent_correlations%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_binding public.hotmart_abandonment_timer_policy_bindings%rowtype;
    v_policy public.followup_policy_versions%rowtype;
    v_existing public.hotmart_abandonment_reevaluations%rowtype;
    v_delay_seconds integer;
    v_reevaluation_id uuid;
begin
    if p_webhook_event_id is null then
        raise exception using
            errcode = '22023',
            message = 'hotmart_abandonment_reevaluation_event_required';
    end if;

    select correlation.* into v_correlation
    from public.hotmart_purchase_intent_correlations correlation
    where correlation.webhook_event_id = p_webhook_event_id;

    if not found
       or v_correlation.outcome <> 'resolved'
       or v_correlation.event_type <> 'PURCHASE_OUT_OF_SHOPPING_CART'
       or v_correlation.purchase_intent_id is null
       or v_correlation.manual_handoff_required then
        return query select 'not_eligible'::text, null::uuid, false;
        return;
    end if;

    select intent.* into strict v_intent
    from public.purchase_intents intent
    where intent.id = v_correlation.purchase_intent_id
    for update;

    select reevaluation.* into v_existing
    from public.hotmart_abandonment_reevaluations reevaluation
    where reevaluation.source_webhook_event_id = p_webhook_event_id;
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

    if v_intent.lifecycle_state <> 'waiting_for_purchase'
       or v_intent.current_classification <> 'confirmed_abandonment' then
        return query select 'not_eligible'::text, null::uuid, false;
        return;
    end if;

    select scope.* into strict v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.id = v_correlation.scope_id;

    if v_scope.tenant_ref is distinct from v_intent.tenant_ref
       or v_scope.funnel_ref is distinct from v_intent.funnel_ref
       or lower(v_scope.purchase_intent_product_ref)
          is distinct from lower(v_intent.product_ref)
       or v_scope.offer_ref is distinct from v_intent.offer_ref then
        raise exception using
            errcode = '23514',
            message = 'hotmart_abandonment_reevaluation_scope_mismatch';
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

    if not found or not v_binding.enabled then
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

    perform 1
    from public.hotmart_abandonment_timer_policy_binding_events event
    where event.binding_id = v_binding.id
      and event.generation = v_binding.generation
      and event.enabled
      and event.policy_key = v_binding.policy_key
      and event.policy_version = v_binding.policy_version
      and event.delay_seconds = v_delay_seconds;
    if not found then
        raise exception using
            errcode = '23514',
            message = 'hotmart_abandonment_timer_policy_snapshot_missing';
    end if;

    select reevaluation.* into v_existing
    from public.hotmart_abandonment_reevaluations reevaluation
    where reevaluation.purchase_intent_id = v_intent.id
      and reevaluation.status = 'scheduled'
    for update;
    if found then
        return query select 'scheduled'::text, v_existing.id, false;
        return;
    end if;

    insert into public.hotmart_abandonment_reevaluations (
        purchase_intent_id,
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
        p_webhook_event_id,
        v_scope.id,
        v_binding.id,
        v_binding.generation,
        v_binding.policy_key,
        v_binding.policy_version,
        v_delay_seconds,
        v_correlation.observed_at,
        v_correlation.observed_at + make_interval(secs => v_delay_seconds),
        'hotmart-abandonment:' || p_webhook_event_id::text
    ) returning id into v_reevaluation_id;

    return query select 'scheduled'::text, v_reevaluation_id, true;
end;
$function$;

create or replace function public.cancel_hotmart_abandonment_reevaluations_for_purchase(
    p_purchase_intent_id uuid,
    p_now timestamptz
)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_intent public.purchase_intents%rowtype;
    v_updated integer;
begin
    if p_purchase_intent_id is null or p_now is null then
        raise exception using
            errcode = '22023',
            message = 'hotmart_abandonment_purchase_cancellation_input_invalid';
    end if;

    select intent.* into strict v_intent
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id
    for update;
    if v_intent.lifecycle_state <> 'purchased' then
        raise exception using
            errcode = '55000',
            message = 'hotmart_abandonment_purchase_cancellation_requires_purchase';
    end if;

    perform 1
    from public.hotmart_abandonment_reevaluations reevaluation
    where reevaluation.purchase_intent_id = p_purchase_intent_id
      and reevaluation.outcome is distinct from 'cancelled_purchased'
    order by reevaluation.id
    for update;

    update public.hotmart_abandonment_reevaluations reevaluation
    set status = 'completed',
        outcome = 'cancelled_purchased',
        completed_at = p_now,
        updated_at = p_now
    where reevaluation.purchase_intent_id = p_purchase_intent_id
      and reevaluation.outcome is distinct from 'cancelled_purchased';
    get diagnostics v_updated = row_count;
    return v_updated;
end;
$function$;

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
      and reevaluation.due_at <= p_now
    order by reevaluation.due_at, reevaluation.id
    limit p_batch_size;
end;
$function$;

create or replace function public.reevaluate_hotmart_abandonment_timer(
    p_reevaluation_id uuid,
    p_now timestamptz
)
returns table (
    reevaluation_id uuid,
    reevaluation_status text,
    reevaluation_outcome text,
    completed_at timestamptz,
    replayed boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_purchase_intent_id uuid;
    v_intent public.purchase_intents%rowtype;
    v_reevaluation public.hotmart_abandonment_reevaluations%rowtype;
    v_outcome text;
begin
    if p_reevaluation_id is null or p_now is null then
        raise exception using
            errcode = '22023',
            message = 'hotmart_abandonment_reevaluation_input_invalid';
    end if;

    select reevaluation.purchase_intent_id into v_purchase_intent_id
    from public.hotmart_abandonment_reevaluations reevaluation
    where reevaluation.id = p_reevaluation_id;
    if not found then
        raise exception using
            errcode = 'P0002',
            message = 'hotmart_abandonment_reevaluation_not_found';
    end if;

    -- Global order: purchase_intent, then its timer.
    select intent.* into strict v_intent
    from public.purchase_intents intent
    where intent.id = v_purchase_intent_id
    for update;

    select reevaluation.* into strict v_reevaluation
    from public.hotmart_abandonment_reevaluations reevaluation
    where reevaluation.id = p_reevaluation_id
      and reevaluation.purchase_intent_id = v_intent.id
    for update;

    if v_reevaluation.status = 'completed' then
        return query select
            v_reevaluation.id,
            v_reevaluation.status,
            v_reevaluation.outcome,
            v_reevaluation.completed_at,
            true;
        return;
    end if;

    if v_reevaluation.due_at > p_now then
        raise exception using
            errcode = '55000',
            message = 'hotmart_abandonment_reevaluation_not_due';
    end if;

    v_outcome := case
        when v_intent.lifecycle_state = 'purchased'
            then 'cancelled_purchased'
        when v_intent.lifecycle_state = 'waiting_for_purchase'
         and v_intent.current_classification = 'confirmed_abandonment'
         and (
             not v_intent.activation_authorized
             or not v_intent.whatsapp_contact_authorized
         )
            then 'blocked_not_authorized'
        when v_intent.lifecycle_state = 'waiting_for_purchase'
         and v_intent.current_classification = 'confirmed_abandonment'
         and v_intent.activation_authorized
         and v_intent.whatsapp_contact_authorized
            then 'blocked_contact_binding_missing'
        else 'cancelled_intent_changed'
    end;

    update public.hotmart_abandonment_reevaluations
    set status = 'completed',
        outcome = v_outcome,
        completed_at = p_now,
        updated_at = p_now
    where id = v_reevaluation.id
    returning * into strict v_reevaluation;

    return query select
        v_reevaluation.id,
        v_reevaluation.status,
        v_reevaluation.outcome,
        v_reevaluation.completed_at,
        false;
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
    v_admission_outcome text;
    v_event_id uuid;
    v_correlation_outcome text;
    v_purchase_intent_id uuid;
begin
    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_event_id
    from public._admit_hotmart_purchase_approved_base(
        p_external_event_id, p_payload
    ) admission;

    if v_admission_outcome <> 'semantic_conflict' then
        perform public._admit_hotmart_purchase_intent_identity(
            v_event_id, p_normalized_email, p_normalized_phone
        );
        select correlation.outcome, correlation.purchase_intent_id
          into strict v_correlation_outcome, v_purchase_intent_id
        from public.correlate_hotmart_purchase_intent(v_event_id) correlation;
        if v_correlation_outcome = 'resolved' then
            perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(
                v_purchase_intent_id,
                clock_timestamp()
            );
        end if;
    end if;

    return query select v_admission_outcome, v_event_id;
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
    v_admission_outcome text;
    v_event_id uuid;
    v_correlation_outcome text;
begin
    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_event_id
    from public._admit_hotmart_cart_abandonment_base(
        p_external_event_id, p_payload
    ) admission;

    if v_admission_outcome <> 'semantic_conflict' then
        perform public._admit_hotmart_purchase_intent_identity(
            v_event_id, p_normalized_email, p_normalized_phone
        );
        select correlation.outcome into strict v_correlation_outcome
        from public.correlate_hotmart_purchase_intent(v_event_id) correlation;
        if v_correlation_outcome = 'resolved' then
            perform public.schedule_hotmart_abandonment_reevaluation(v_event_id);
        end if;
    end if;

    return query select v_admission_outcome, v_event_id;
end;
$function$;

alter table public.hotmart_abandonment_timer_policy_bindings enable row level security;
alter table public.hotmart_abandonment_timer_policy_binding_events enable row level security;
alter table public.hotmart_abandonment_reevaluations enable row level security;
alter table public.hotmart_abandonment_reevaluation_events enable row level security;

revoke all on table public.hotmart_abandonment_timer_policy_bindings from public;
revoke all on table public.hotmart_abandonment_timer_policy_binding_events from public;
revoke all on table public.hotmart_abandonment_reevaluations from public;
revoke all on table public.hotmart_abandonment_reevaluation_events from public;
revoke all on function public.validate_hotmart_abandonment_timer_policy_binding() from public;
revoke all on function public.record_hotmart_abandonment_timer_policy_binding() from public;
revoke all on function public.protect_hotmart_abandonment_timer_policy_binding_event() from public;
revoke all on function public.protect_hotmart_abandonment_reevaluation() from public;
revoke all on function public.record_hotmart_abandonment_reevaluation_event() from public;
revoke all on function public.protect_hotmart_abandonment_reevaluation_event() from public;
revoke all on function public.schedule_hotmart_abandonment_reevaluation(uuid) from public;
revoke all on function public.cancel_hotmart_abandonment_reevaluations_for_purchase(uuid, timestamptz) from public;
revoke all on function public.list_due_hotmart_abandonment_reevaluations(timestamptz, integer) from public;
revoke all on function public.reevaluate_hotmart_abandonment_timer(uuid, timestamptz) from public;

do $acl$
declare
    v_role text;
begin
    for v_role in
        select role.rolname
        from pg_roles role
        where role.rolname in ('anon', 'authenticated', 'service_role')
    loop
        execute format(
            'revoke all on table public.hotmart_abandonment_timer_policy_bindings from %I',
            v_role
        );
        execute format(
            'revoke all on table public.hotmart_abandonment_timer_policy_binding_events from %I',
            v_role
        );
        execute format(
            'revoke all on table public.hotmart_abandonment_reevaluations from %I',
            v_role
        );
        execute format(
            'revoke all on table public.hotmart_abandonment_reevaluation_events from %I',
            v_role
        );
        execute format(
            'revoke all on function public.validate_hotmart_abandonment_timer_policy_binding() from %I',
            v_role
        );
        execute format(
            'revoke all on function public.record_hotmart_abandonment_timer_policy_binding() from %I',
            v_role
        );
        execute format(
            'revoke all on function public.protect_hotmart_abandonment_timer_policy_binding_event() from %I',
            v_role
        );
        execute format(
            'revoke all on function public.protect_hotmart_abandonment_reevaluation() from %I',
            v_role
        );
        execute format(
            'revoke all on function public.record_hotmart_abandonment_reevaluation_event() from %I',
            v_role
        );
        execute format(
            'revoke all on function public.protect_hotmart_abandonment_reevaluation_event() from %I',
            v_role
        );
        execute format(
            'revoke all on function public.schedule_hotmart_abandonment_reevaluation(uuid) from %I',
            v_role
        );
        execute format(
            'revoke all on function public.cancel_hotmart_abandonment_reevaluations_for_purchase(uuid, timestamptz) from %I',
            v_role
        );
        execute format(
            'revoke all on function public.list_due_hotmart_abandonment_reevaluations(timestamptz, integer) from %I',
            v_role
        );
        execute format(
            'revoke all on function public.reevaluate_hotmart_abandonment_timer(uuid, timestamptz) from %I',
            v_role
        );
    end loop;

    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.list_due_hotmart_abandonment_reevaluations(
            timestamptz, integer
        ) to service_role;
        grant execute on function public.reevaluate_hotmart_abandonment_timer(
            uuid, timestamptz
        ) to service_role;
        grant execute on function public.admit_and_correlate_hotmart_purchase_approved(
            text, jsonb, text, text
        ) to service_role;
        grant execute on function public.admit_and_correlate_hotmart_cart_abandonment(
            text, jsonb, text, text
        ) to service_role;
    end if;
end;
$acl$;

commit;
