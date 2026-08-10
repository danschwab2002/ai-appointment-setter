-- Perímetro durable y default-off para el piloto V1 de Lancemos.
-- Esta migración agrega configuración y entrypoints, pero no publica valores
-- reales ni activa outbound.

begin;

create table public.pilot_scope_versions (
    scope_key text not null check (length(btrim(scope_key)) > 0),
    version integer not null check (version > 0),
    status text not null check (status = any (array['draft', 'published'])),
    tenant_key text not null check (tenant_key = 'lancemos'),
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    channel text not null check (channel = 'whatsapp'),
    channel_provider text not null check (length(btrim(channel_provider)) > 0),
    channel_account_ref text not null check (length(btrim(channel_account_ref)) > 0),
    source text not null check (source = 'hotmart'),
    source_event_type text not null
        check (source_event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'),
    external_product_id text not null check (length(btrim(external_product_id)) > 0),
    offer_code text not null check (length(btrim(offer_code)) > 0),
    purpose text not null check (purpose = 'cart_recovery'),
    policy_key text not null,
    policy_version integer not null check (policy_version > 0),
    timezone text not null check (length(btrim(timezone)) > 0),
    max_cohort_contacts integer not null check (max_cohort_contacts > 0),
    max_outbound_request_starts_total integer not null
        check (max_outbound_request_starts_total > 0),
    max_outbound_request_starts_per_day integer not null
        check (max_outbound_request_starts_per_day > 0),
    approved_by text,
    approved_at timestamptz,
    published_at timestamptz,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    primary key (scope_key, version),
    foreign key (policy_key, policy_version)
        references public.followup_policy_versions(policy_key, version)
        on delete restrict,
    check (
        status <> 'published'
        or (
            approved_by is not null
            and length(btrim(approved_by)) > 0
            and approved_at is not null
            and published_at is not null
        )
    ),
    check (
        max_outbound_request_starts_per_day
        <= max_outbound_request_starts_total
    )
);

alter table public.pilot_scope_versions enable row level security;

create or replace function public.validate_pilot_scope_version()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        if old.status = 'published' then
            raise exception using
                errcode = '55000',
                message = 'published_pilot_scope_is_immutable';
        end if;
        return old;
    end if;

    if tg_op = 'UPDATE' and old.status = 'published' then
        raise exception using
            errcode = '55000',
            message = 'published_pilot_scope_is_immutable';
    end if;

    if not exists (
        select 1 from pg_timezone_names tz where tz.name = new.timezone
    ) then
        raise exception using
            errcode = '22023',
            message = 'pilot_scope_timezone_invalid';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('pilot_scope_timezone:' || new.scope_key, 0)
    );
    if exists (
        select 1
        from public.pilot_scope_versions existing
        where existing.scope_key = new.scope_key
          and existing.version <> new.version
          and existing.timezone <> new.timezone
    ) then
        raise exception using
            errcode = '23514',
            message = 'pilot_scope_timezone_must_remain_constant';
    end if;

    if new.status = 'published' and not exists (
        select 1
        from public.followup_policy_versions policy
        where policy.policy_key = new.policy_key
          and policy.version = new.policy_version
          and policy.status = 'published'
          and policy.purpose = 'cart_recovery'
    ) then
        raise exception using
            errcode = '23514',
            message = 'pilot_scope_policy_not_published';
    end if;

    return new;
end;
$function$;

create trigger pilot_scope_versions_validate
before insert or update or delete on public.pilot_scope_versions
for each row execute function public.validate_pilot_scope_version();

create trigger pilot_scope_versions_set_updated_at
before update on public.pilot_scope_versions
for each row execute function public.set_updated_at();

create table public.pilot_runtime_controls (
    scope_key text primary key,
    scope_version integer not null check (scope_version > 0),
    runtime_state text not null
        check (runtime_state = any (array['inactive', 'armed', 'paused', 'closed'])),
    generation bigint not null default 0 check (generation >= 0),
    changed_by text not null check (length(btrim(changed_by)) > 0),
    change_reason text not null check (length(btrim(change_reason)) > 0),
    changed_at timestamptz not null default clock_timestamp(),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version)
        on delete restrict
);

alter table public.pilot_runtime_controls enable row level security;

create or replace function public.validate_pilot_runtime_control_transition()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'INSERT' then
        if new.runtime_state <> 'inactive' or new.generation <> 0 then
            raise exception using
                errcode = '23514',
                message = 'pilot_runtime_must_start_inactive';
        end if;
        return new;
    end if;

    if old.runtime_state = 'closed'
       and (
           new.runtime_state is distinct from old.runtime_state
           or new.scope_version is distinct from old.scope_version
           or new.generation is distinct from old.generation
       ) then
        raise exception using
            errcode = '55000',
            message = 'closed_pilot_runtime_is_terminal';
    end if;

    if new.generation <> old.generation + 1 then
        raise exception using
            errcode = '23514',
            message = 'pilot_runtime_generation_must_increment';
    end if;

    if new.scope_version is distinct from old.scope_version then
        if old.runtime_state not in ('inactive', 'paused')
           or new.runtime_state <> 'inactive' then
            raise exception using
                errcode = '55000',
                message = 'pilot_scope_version_change_requires_pause';
        end if;
        return new;
    end if;

    if old.runtime_state = 'inactive'
       and new.runtime_state not in ('inactive', 'armed', 'paused', 'closed') then
        raise exception using errcode = '23514', message = 'pilot_runtime_transition_invalid';
    elsif old.runtime_state = 'armed'
       and new.runtime_state not in ('armed', 'paused', 'closed') then
        raise exception using errcode = '23514', message = 'pilot_runtime_transition_invalid';
    elsif old.runtime_state = 'paused'
       and new.runtime_state not in ('paused', 'armed', 'closed') then
        raise exception using errcode = '23514', message = 'pilot_runtime_transition_invalid';
    end if;

    return new;
end;
$function$;

create trigger pilot_runtime_controls_validate
before insert or update on public.pilot_runtime_controls
for each row execute function public.validate_pilot_runtime_control_transition();

create trigger pilot_runtime_controls_set_updated_at
before update on public.pilot_runtime_controls
for each row execute function public.set_updated_at();

create table public.pilot_cohort_memberships (
    scope_key text not null,
    scope_version integer not null check (scope_version > 0),
    contact_id uuid not null references public.contacts(id) on delete restrict,
    member_status text not null check (member_status = any (array['active', 'removed'])),
    enrolled_by text not null check (length(btrim(enrolled_by)) > 0),
    enrollment_reason text not null check (length(btrim(enrollment_reason)) > 0),
    enrolled_at timestamptz not null default clock_timestamp(),
    removed_by text,
    removal_reason text,
    removed_at timestamptz,
    last_runtime_generation bigint not null check (last_runtime_generation >= 0),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    primary key (scope_key, scope_version, contact_id),
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version)
        on delete restrict,
    check (
        (member_status = 'active'
         and removed_by is null and removal_reason is null and removed_at is null)
        or
        (member_status = 'removed'
         and removed_by is not null and length(btrim(removed_by)) > 0
         and removal_reason is not null and length(btrim(removal_reason)) > 0
         and removed_at is not null)
    )
);

create index pilot_cohort_memberships_active_idx
on public.pilot_cohort_memberships(scope_key, scope_version, contact_id)
where member_status = 'active';

alter table public.pilot_cohort_memberships enable row level security;

create trigger pilot_cohort_memberships_set_updated_at
before update on public.pilot_cohort_memberships
for each row execute function public.set_updated_at();

create table public.pilot_outbound_request_authorizations (
    id uuid primary key default gen_random_uuid(),
    scope_key text not null,
    scope_version integer not null check (scope_version > 0),
    action_id uuid not null references public.scheduled_actions(id) on delete restrict,
    attempt_id uuid not null unique
        references public.followup_delivery_attempts(id) on delete restrict,
    contact_id uuid not null references public.contacts(id) on delete restrict,
    local_budget_date date not null,
    runtime_generation bigint not null check (runtime_generation >= 0),
    reason_code text not null check (reason_code = 'pilot_request_start_authorized'),
    authorized_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp(),
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version)
        on delete restrict
);

create index pilot_outbound_authorizations_total_idx
on public.pilot_outbound_request_authorizations(scope_key, scope_version);

create index pilot_outbound_authorizations_daily_idx
on public.pilot_outbound_request_authorizations(
    scope_key, scope_version, local_budget_date
);

alter table public.pilot_outbound_request_authorizations enable row level security;

create table public.pilot_control_events (
    id uuid primary key default gen_random_uuid(),
    scope_key text not null,
    scope_version integer not null check (scope_version > 0),
    event_type text not null check (event_type = any (array[
        'pilot_runtime_state_changed',
        'pilot_scope_version_activated',
        'pilot_cohort_member_enrolled',
        'pilot_cohort_member_removed',
        'pilot_outbound_request_authorized'
    ])),
    runtime_generation bigint not null check (runtime_generation >= 0),
    contact_id uuid references public.contacts(id) on delete restrict,
    action_id uuid references public.scheduled_actions(id) on delete restrict,
    attempt_id uuid references public.followup_delivery_attempts(id) on delete restrict,
    actor text not null check (length(btrim(actor)) > 0),
    reason_code text not null check (length(btrim(reason_code)) > 0),
    data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default clock_timestamp(),
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version)
        on delete restrict
);

create index pilot_control_events_scope_time_idx
on public.pilot_control_events(scope_key, scope_version, created_at desc);

alter table public.pilot_control_events enable row level security;

create or replace function public.reject_pilot_append_only_mutation()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_table_name = 'pilot_outbound_request_authorizations' then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_authorization_is_append_only';
    end if;
    raise exception using
        errcode = '55000',
        message = 'pilot_control_event_is_append_only';
end;
$function$;

create trigger pilot_outbound_authorizations_append_only
before update or delete on public.pilot_outbound_request_authorizations
for each row execute function public.reject_pilot_append_only_mutation();

create trigger pilot_control_events_append_only
before update or delete on public.pilot_control_events
for each row execute function public.reject_pilot_append_only_mutation();

create or replace function public.activate_lancemos_pilot_scope_version(
    p_scope_key text,
    p_target_scope_version integer,
    p_expected_generation bigint,
    p_actor text,
    p_reason text
)
returns table (
    scope_version integer,
    runtime_state text,
    generation bigint,
    changed boolean,
    reason_code text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_control public.pilot_runtime_controls%rowtype;
    v_target public.pilot_scope_versions%rowtype;
    v_previous_version integer;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_target_scope_version is null or p_target_scope_version < 1
       or p_expected_generation is null or p_expected_generation < 0
       or p_actor is null or btrim(p_actor) = ''
       or p_reason is null or btrim(p_reason) = '' then
        raise exception using
            errcode = '22023',
            message = 'pilot_scope_activation_input_invalid';
    end if;

    select target.* into strict v_target
    from public.pilot_scope_versions target
    where target.scope_key = p_scope_key
      and target.version = p_target_scope_version
      and target.status = 'published';

    select control.* into strict v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;

    if v_control.generation <> p_expected_generation then
        raise exception using
            errcode = '40001',
            message = 'pilot_runtime_generation_mismatch';
    end if;
    if v_control.scope_version = p_target_scope_version then
        return query select
            v_control.scope_version,
            v_control.runtime_state,
            v_control.generation,
            false,
            'pilot_scope_version_unchanged'::text;
        return;
    end if;
    if v_control.runtime_state not in ('inactive', 'paused') then
        raise exception using
            errcode = '55000',
            message = case
                when v_control.runtime_state = 'closed'
                    then 'closed_pilot_runtime_is_terminal'
                else 'pilot_scope_version_change_requires_pause'
            end;
    end if;

    v_previous_version := v_control.scope_version;
    update public.pilot_runtime_controls control
    set scope_version = p_target_scope_version,
        runtime_state = 'inactive',
        generation = control.generation + 1,
        changed_by = btrim(p_actor),
        change_reason = btrim(p_reason),
        changed_at = clock_timestamp()
    where control.scope_key = p_scope_key
    returning control.* into v_control;

    insert into public.pilot_control_events (
        scope_key, scope_version, event_type, runtime_generation,
        actor, reason_code, data
    ) values (
        p_scope_key, p_target_scope_version, 'pilot_scope_version_activated',
        v_control.generation, btrim(p_actor), 'pilot_scope_version_activated',
        jsonb_build_object(
            'from_version', v_previous_version,
            'to_version', p_target_scope_version,
            'runtime_state', 'inactive'
        )
    );

    return query select
        v_control.scope_version,
        v_control.runtime_state,
        v_control.generation,
        true,
        'pilot_scope_version_activated'::text;
end;
$function$;

create or replace function public.set_lancemos_pilot_runtime_state(
    p_scope_key text,
    p_scope_version integer,
    p_expected_generation bigint,
    p_target_state text,
    p_actor text,
    p_reason text
)
returns table (
    runtime_state text,
    generation bigint,
    changed boolean,
    reason_code text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_control public.pilot_runtime_controls%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_expected_generation is null or p_expected_generation < 0
       or p_target_state is null
       or p_target_state not in ('inactive', 'armed', 'paused', 'closed')
       or p_actor is null or btrim(p_actor) = ''
       or p_reason is null or btrim(p_reason) = '' then
        raise exception using errcode = '22023', message = 'pilot_runtime_input_invalid';
    end if;

    select scope.* into strict v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';

    select control.* into strict v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;

    if v_control.scope_version <> p_scope_version then
        raise exception using errcode = '55000', message = 'pilot_scope_version_mismatch';
    end if;
    if v_control.generation <> p_expected_generation then
        raise exception using errcode = '40001', message = 'pilot_runtime_generation_mismatch';
    end if;

    if v_control.runtime_state = p_target_state then
        return query select
            v_control.runtime_state,
            v_control.generation,
            false,
            'pilot_runtime_state_unchanged'::text;
        return;
    end if;

    if v_control.runtime_state = 'closed' then
        raise exception using errcode = '55000', message = 'closed_pilot_runtime_is_terminal';
    end if;
    if v_control.runtime_state = 'armed' and p_target_state = 'inactive' then
        raise exception using errcode = '55000', message = 'pilot_runtime_transition_invalid';
    end if;
    if v_control.runtime_state = 'paused' and p_target_state = 'inactive' then
        raise exception using errcode = '55000', message = 'pilot_runtime_transition_invalid';
    end if;

    update public.pilot_runtime_controls control
    set runtime_state = p_target_state,
        generation = control.generation + 1,
        changed_by = btrim(p_actor),
        change_reason = btrim(p_reason),
        changed_at = clock_timestamp()
    where control.scope_key = p_scope_key
    returning control.* into v_control;

    insert into public.pilot_control_events (
        scope_key, scope_version, event_type, runtime_generation,
        actor, reason_code, data
    ) values (
        p_scope_key, p_scope_version, 'pilot_runtime_state_changed',
        v_control.generation, btrim(p_actor), 'pilot_runtime_state_changed',
        jsonb_build_object('to_state', p_target_state)
    );

    return query select
        v_control.runtime_state,
        v_control.generation,
        true,
        'pilot_runtime_state_changed'::text;
end;
$function$;

create or replace function public.set_lancemos_pilot_cohort_member(
    p_scope_key text,
    p_scope_version integer,
    p_contact_id uuid,
    p_expected_generation bigint,
    p_target_status text,
    p_actor text,
    p_reason text
)
returns table (
    member_status text,
    generation bigint,
    active_member_count integer,
    changed boolean,
    reason_code text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_control public.pilot_runtime_controls%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
    v_member public.pilot_cohort_memberships%rowtype;
    v_count integer;
    v_event_type text;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_contact_id is null
       or p_expected_generation is null or p_expected_generation < 0
       or p_target_status is null or p_target_status not in ('active', 'removed')
       or p_actor is null or btrim(p_actor) = ''
       or p_reason is null or btrim(p_reason) = '' then
        raise exception using errcode = '22023', message = 'pilot_cohort_input_invalid';
    end if;

    select scope.* into strict v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';

    select control.* into strict v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;

    if v_control.scope_version <> p_scope_version then
        raise exception using errcode = '55000', message = 'pilot_scope_version_mismatch';
    end if;
    if v_control.generation <> p_expected_generation then
        raise exception using errcode = '40001', message = 'pilot_runtime_generation_mismatch';
    end if;
    if v_control.runtime_state = 'closed' then
        raise exception using errcode = '55000', message = 'closed_pilot_runtime_is_terminal';
    end if;

    perform 1 from public.contacts contact where contact.id = p_contact_id for update;
    if not found then
        raise exception using errcode = '23503', message = 'pilot_contact_not_found';
    end if;

    select member.* into v_member
    from public.pilot_cohort_memberships member
    where member.scope_key = p_scope_key
      and member.scope_version = p_scope_version
      and member.contact_id = p_contact_id
    for update;

    select count(*)::integer into v_count
    from public.pilot_cohort_memberships member
    where member.scope_key = p_scope_key
      and member.scope_version = p_scope_version
      and member.member_status = 'active';

    if found and v_member.member_status = p_target_status then
        return query select
            v_member.member_status,
            v_control.generation,
            v_count,
            false,
            'pilot_cohort_member_unchanged'::text;
        return;
    end if;

    if p_target_status = 'active' and v_count >= v_scope.max_cohort_contacts then
        return query select
            coalesce(v_member.member_status, 'removed'),
            v_control.generation,
            v_count,
            false,
            'pilot_cohort_limit_reached'::text;
        return;
    end if;

    if p_target_status = 'removed' and v_member.contact_id is null then
        return query select
            'removed'::text,
            v_control.generation,
            v_count,
            false,
            'pilot_cohort_member_unchanged'::text;
        return;
    end if;

    update public.pilot_runtime_controls control
    set generation = control.generation + 1,
        changed_by = btrim(p_actor),
        change_reason = btrim(p_reason),
        changed_at = clock_timestamp()
    where control.scope_key = p_scope_key
    returning control.* into v_control;

    if p_target_status = 'active' then
        insert into public.pilot_cohort_memberships (
            scope_key, scope_version, contact_id, member_status,
            enrolled_by, enrollment_reason, enrolled_at,
            removed_by, removal_reason, removed_at,
            last_runtime_generation
        ) values (
            p_scope_key, p_scope_version, p_contact_id, 'active',
            btrim(p_actor), btrim(p_reason), clock_timestamp(),
            null, null, null, v_control.generation
        )
        on conflict (scope_key, scope_version, contact_id) do update
        set member_status = 'active',
            enrolled_by = excluded.enrolled_by,
            enrollment_reason = excluded.enrollment_reason,
            enrolled_at = excluded.enrolled_at,
            removed_by = null,
            removal_reason = null,
            removed_at = null,
            last_runtime_generation = excluded.last_runtime_generation
        returning * into v_member;
        v_count := v_count + 1;
        v_event_type := 'pilot_cohort_member_enrolled';
    else
        update public.pilot_cohort_memberships member
        set member_status = 'removed',
            removed_by = btrim(p_actor),
            removal_reason = btrim(p_reason),
            removed_at = clock_timestamp(),
            last_runtime_generation = v_control.generation
        where member.scope_key = p_scope_key
          and member.scope_version = p_scope_version
          and member.contact_id = p_contact_id
        returning * into v_member;
        v_count := v_count - 1;
        v_event_type := 'pilot_cohort_member_removed';
    end if;

    insert into public.pilot_control_events (
        scope_key, scope_version, event_type, runtime_generation,
        contact_id, actor, reason_code
    ) values (
        p_scope_key, p_scope_version, v_event_type, v_control.generation,
        p_contact_id, btrim(p_actor), v_event_type
    );

    return query select
        v_member.member_status,
        v_control.generation,
        v_count,
        true,
        v_event_type;
end;
$function$;

create or replace function public.evaluate_lancemos_pilot_scope(
    p_scope_key text,
    p_scope_version integer,
    p_tenant_key text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_channel_provider text,
    p_channel_account_ref text,
    p_source text,
    p_source_event_type text,
    p_external_product_id text,
    p_offer_code text,
    p_contact_id uuid
)
returns table (
    allowed boolean,
    reason_code text,
    runtime_generation bigint
)
language plpgsql
security definer
stable
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
    v_reason text;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_tenant_key is null or btrim(p_tenant_key) = ''
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_channel_provider is null or btrim(p_channel_provider) = ''
       or p_channel_account_ref is null or btrim(p_channel_account_ref) = ''
       or p_source is null or btrim(p_source) = ''
       or p_source_event_type is null or btrim(p_source_event_type) = ''
       or p_external_product_id is null or btrim(p_external_product_id) = ''
       or p_offer_code is null or btrim(p_offer_code) = ''
       or p_contact_id is null then
        return query select false, 'pilot_scope_input_invalid'::text, null::bigint;
        return;
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found then
        return query select false, 'pilot_scope_not_published'::text, null::bigint;
        return;
    end if;

    select control.* into v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key;
    if not found or v_control.scope_version <> p_scope_version then
        return query select false, 'pilot_scope_version_mismatch'::text,
            coalesce(v_control.generation, null::bigint);
        return;
    end if;

    if v_control.runtime_state <> 'armed' then
        v_reason := 'pilot_runtime_not_armed';
    elsif p_tenant_key <> v_scope.tenant_key then
        v_reason := 'pilot_tenant_mismatch';
    elsif p_chatwoot_account_id <> v_scope.chatwoot_account_id then
        v_reason := 'pilot_chatwoot_account_mismatch';
    elsif p_chatwoot_inbox_id <> v_scope.chatwoot_inbox_id then
        v_reason := 'pilot_chatwoot_inbox_mismatch';
    elsif p_channel_provider <> v_scope.channel_provider
       or p_channel_account_ref <> v_scope.channel_account_ref then
        v_reason := 'pilot_channel_account_mismatch';
    elsif p_source <> v_scope.source
       or p_source_event_type <> v_scope.source_event_type then
        v_reason := 'pilot_source_event_mismatch';
    elsif p_external_product_id <> v_scope.external_product_id then
        v_reason := 'pilot_product_mismatch';
    elsif p_offer_code <> v_scope.offer_code then
        v_reason := 'pilot_offer_mismatch';
    elsif not exists (
        select 1 from public.pilot_cohort_memberships member
        where member.scope_key = p_scope_key
          and member.scope_version = p_scope_version
          and member.contact_id = p_contact_id
          and member.member_status = 'active'
    ) then
        v_reason := 'pilot_contact_not_in_cohort';
    else
        v_reason := 'pilot_scope_allowed';
    end if;

    return query select
        v_reason = 'pilot_scope_allowed',
        v_reason,
        v_control.generation;
end;
$function$;

create or replace function public.authorize_lancemos_pilot_request_start(
    p_scope_key text,
    p_scope_version integer,
    p_tenant_key text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_channel_provider text,
    p_channel_account_ref text,
    p_source text,
    p_source_event_type text,
    p_external_product_id text,
    p_offer_code text,
    p_contact_id uuid,
    p_action_id uuid,
    p_attempt_id uuid,
    p_now timestamptz
)
returns table (
    authorized boolean,
    reason_code text,
    runtime_generation bigint,
    request_authorization_id uuid,
    replayed boolean
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
    v_existing public.pilot_outbound_request_authorizations%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_identity public.channel_identities%rowtype;
    v_local_date date;
    v_total integer;
    v_daily integer;
    v_authorization_id uuid;
    v_authorized_at timestamptz;
    v_control_exists boolean;
    v_reason text;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_tenant_key is null or btrim(p_tenant_key) = ''
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_channel_provider is null or btrim(p_channel_provider) = ''
       or p_channel_account_ref is null or btrim(p_channel_account_ref) = ''
       or p_source is null or btrim(p_source) = ''
       or p_source_event_type is null or btrim(p_source_event_type) = ''
       or p_external_product_id is null or btrim(p_external_product_id) = ''
       or p_offer_code is null or btrim(p_offer_code) = ''
       or p_contact_id is null or p_action_id is null or p_attempt_id is null
       or p_now is null then
        return query select false, 'pilot_scope_input_invalid'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found then
        return query select false, 'pilot_scope_not_published'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select authrow.* into v_existing
    from public.pilot_outbound_request_authorizations authrow
    where authrow.attempt_id = p_attempt_id;

    if found and (
        v_existing.scope_key <> p_scope_key
        or v_existing.scope_version <> p_scope_version
        or v_existing.action_id <> p_action_id
        or v_existing.contact_id <> p_contact_id
    ) then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_existing.runtime_generation, null::uuid, false;
        return;
    end if;

    if p_tenant_key <> v_scope.tenant_key then
        v_reason := 'pilot_tenant_mismatch';
    elsif p_chatwoot_account_id <> v_scope.chatwoot_account_id then
        v_reason := 'pilot_chatwoot_account_mismatch';
    elsif p_chatwoot_inbox_id <> v_scope.chatwoot_inbox_id then
        v_reason := 'pilot_chatwoot_inbox_mismatch';
    elsif p_channel_provider <> v_scope.channel_provider
       or p_channel_account_ref <> v_scope.channel_account_ref then
        v_reason := 'pilot_channel_account_mismatch';
    elsif p_source <> v_scope.source
       or p_source_event_type <> v_scope.source_event_type then
        v_reason := 'pilot_source_event_mismatch';
    elsif p_external_product_id <> v_scope.external_product_id then
        v_reason := 'pilot_product_mismatch';
    elsif p_offer_code <> v_scope.offer_code then
        v_reason := 'pilot_offer_mismatch';
    else
        v_reason := null;
    end if;

    if v_reason is not null then
        return query select false, v_reason,
            case when v_existing.id is null then null else v_existing.runtime_generation end,
            null::uuid, false;
        return;
    end if;

    if v_existing.id is not null then
        return query select true, 'pilot_request_start_authorized'::text,
            v_existing.runtime_generation, v_existing.id, true;
        return;
    end if;

    v_authorized_at := clock_timestamp();
    if p_now < v_authorized_at - interval '5 minutes'
       or p_now > v_authorized_at + interval '5 minutes' then
        return query select false, 'pilot_request_time_invalid'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select control.* into v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;
    v_control_exists := found;

    select authrow.* into v_existing
    from public.pilot_outbound_request_authorizations authrow
    where authrow.attempt_id = p_attempt_id;
    if found then
        if v_existing.scope_key <> p_scope_key
           or v_existing.scope_version <> p_scope_version
           or v_existing.action_id <> p_action_id
           or v_existing.contact_id <> p_contact_id then
            return query select false, 'pilot_attempt_mismatch'::text,
                v_existing.runtime_generation, null::uuid, false;
            return;
        end if;
        return query select true, 'pilot_request_start_authorized'::text,
            v_existing.runtime_generation, v_existing.id, true;
        return;
    end if;

    if not v_control_exists or v_control.scope_version <> p_scope_version then
        return query select false, 'pilot_scope_version_mismatch'::text,
            coalesce(v_control.generation, null::bigint), null::uuid, false;
        return;
    end if;

    if v_control.runtime_state <> 'armed' then
        return query select false, 'pilot_runtime_not_armed'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    if not exists (
        select 1 from public.pilot_cohort_memberships member
        where member.scope_key = p_scope_key
          and member.scope_version = p_scope_version
          and member.contact_id = p_contact_id
          and member.member_status = 'active'
    ) then
        return query select false, 'pilot_contact_not_in_cohort'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select attempt.* into v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = p_attempt_id
      and attempt.action_id = p_action_id;
    if not found or v_attempt.phase <> 'reserved' or v_attempt.outcome is not null then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select recovery_case.* into v_case
    from public.scheduled_actions action
    join public.recovery_cases recovery_case
      on recovery_case.id = action.recovery_case_id
    where action.id = p_action_id
      and recovery_case.contact_id = p_contact_id;
    if not found
       or v_case.external_product_id <> p_external_product_id
       or v_case.offer_code is distinct from p_offer_code
       or v_case.source <> p_source
       or v_case.policy_key <> v_scope.policy_key
       or v_case.policy_version <> v_scope.policy_version
       or v_case.selected_channel_identity_id is null then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.contact_id = p_contact_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active';
    if not found
       or v_identity.account_id <> 'chatwoot:' || p_chatwoot_account_id::text
       or v_identity.metadata ->> 'inbox_id' <> p_chatwoot_inbox_id::text then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    v_local_date := (v_authorized_at at time zone v_scope.timezone)::date;

    select count(*)::integer into v_total
    from public.pilot_outbound_request_authorizations authrow
    where authrow.scope_key = p_scope_key;
    if v_total >= v_scope.max_outbound_request_starts_total then
        return query select false, 'pilot_total_budget_exhausted'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select count(*)::integer into v_daily
    from public.pilot_outbound_request_authorizations authrow
    where authrow.scope_key = p_scope_key
      and authrow.local_budget_date = v_local_date;
    if v_daily >= v_scope.max_outbound_request_starts_per_day then
        return query select false, 'pilot_daily_budget_exhausted'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    insert into public.pilot_outbound_request_authorizations (
        scope_key, scope_version, action_id, attempt_id, contact_id,
        local_budget_date, runtime_generation, reason_code, authorized_at
    ) values (
        p_scope_key, p_scope_version, p_action_id, p_attempt_id, p_contact_id,
        v_local_date, v_control.generation,
        'pilot_request_start_authorized', v_authorized_at
    ) returning id into v_authorization_id;

    insert into public.pilot_control_events (
        scope_key, scope_version, event_type, runtime_generation,
        contact_id, action_id, attempt_id, actor, reason_code,
        data
    ) values (
        p_scope_key, p_scope_version, 'pilot_outbound_request_authorized',
        v_control.generation, p_contact_id, p_action_id, p_attempt_id,
        'system', 'pilot_request_start_authorized',
        jsonb_build_object('local_budget_date', v_local_date)
    );

    return query select true, 'pilot_request_start_authorized'::text,
        v_control.generation, v_authorization_id, false;
end;
$function$;

revoke all on table public.pilot_scope_versions from public;
revoke all on table public.pilot_runtime_controls from public;
revoke all on table public.pilot_cohort_memberships from public;
revoke all on table public.pilot_outbound_request_authorizations from public;
revoke all on table public.pilot_control_events from public;

revoke execute on function public.validate_pilot_scope_version() from public;
revoke execute on function public.validate_pilot_runtime_control_transition() from public;
revoke execute on function public.reject_pilot_append_only_mutation() from public;
revoke execute on function public.activate_lancemos_pilot_scope_version(
    text, integer, bigint, text, text
) from public;
revoke execute on function public.set_lancemos_pilot_runtime_state(
    text, integer, bigint, text, text, text
) from public;
revoke execute on function public.set_lancemos_pilot_cohort_member(
    text, integer, uuid, bigint, text, text, text
) from public;
revoke execute on function public.evaluate_lancemos_pilot_scope(
    text, integer, text, bigint, bigint, text, text, text, text, text, text, uuid
) from public;
revoke execute on function public.authorize_lancemos_pilot_request_start(
    text, integer, text, bigint, bigint, text, text, text, text, text, text,
    uuid, uuid, uuid, timestamptz
) from public;

do $roles$
declare
    v_role text;
begin
    for v_role in
        select role.rolname
        from pg_roles role
        where role.rolname in ('anon', 'authenticated', 'service_role')
    loop
        execute format(
            'revoke all on table public.pilot_scope_versions from %I', v_role
        );
        execute format(
            'revoke all on table public.pilot_runtime_controls from %I', v_role
        );
        execute format(
            'revoke all on table public.pilot_cohort_memberships from %I', v_role
        );
        execute format(
            'revoke all on table public.pilot_outbound_request_authorizations from %I',
            v_role
        );
        execute format(
            'revoke all on table public.pilot_control_events from %I', v_role
        );
        execute format(
            'revoke execute on function public.validate_pilot_scope_version() from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.validate_pilot_runtime_control_transition() from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.reject_pilot_append_only_mutation() from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.activate_lancemos_pilot_scope_version(text, integer, bigint, text, text) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.set_lancemos_pilot_runtime_state(text, integer, bigint, text, text, text) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.set_lancemos_pilot_cohort_member(text, integer, uuid, bigint, text, text, text) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.evaluate_lancemos_pilot_scope(text, integer, text, bigint, bigint, text, text, text, text, text, text, uuid) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.authorize_lancemos_pilot_request_start(text, integer, text, bigint, bigint, text, text, text, text, text, text, uuid, uuid, uuid, timestamptz) from %I',
            v_role
        );
    end loop;

    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.activate_lancemos_pilot_scope_version(
            text, integer, bigint, text, text
        ) to service_role;
        grant execute on function public.set_lancemos_pilot_runtime_state(
            text, integer, bigint, text, text, text
        ) to service_role;
        grant execute on function public.set_lancemos_pilot_cohort_member(
            text, integer, uuid, bigint, text, text, text
        ) to service_role;
        grant execute on function public.evaluate_lancemos_pilot_scope(
            text, integer, text, bigint, bigint, text, text, text, text, text,
            text, uuid
        ) to service_role;
        grant execute on function public.authorize_lancemos_pilot_request_start(
            text, integer, text, bigint, bigint, text, text, text, text, text,
            text, uuid, uuid, uuid, timestamptz
        ) to service_role;
    end if;
end;
$roles$;

commit;
