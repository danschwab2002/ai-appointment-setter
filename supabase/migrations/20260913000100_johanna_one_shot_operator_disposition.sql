-- Preserve ambiguous delivery truth while allowing an audited operator disposition
-- after the external Chatwoot contact was deliberately deleted before reconciliation.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

create table public.johanna_one_shot_operator_dispositions (
    id uuid primary key default gen_random_uuid(),
    command_id uuid not null unique
        references public.johanna_abandonment_one_shot_commands(id) on delete restrict,
    disposition_code text not null
        check (disposition_code = 'unverifiable_external_contact_deleted'),
    evidence_code text not null
        check (evidence_code = 'operator_confirmed_chatwoot_contact_deleted'),
    operator_ref text not null
        check (operator_ref ~ '^[a-z0-9][a-z0-9:_-]{2,63}$'),
    semantic_fingerprint text not null
        check (semantic_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz not null default clock_timestamp()
);

alter table public.johanna_one_shot_operator_dispositions enable row level security;

create function public.protect_johanna_one_shot_operator_disposition()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using
        errcode = '55000',
        message = 'johanna_one_shot_operator_disposition_immutable';
end;
$function$;

create trigger protect_johanna_one_shot_operator_disposition
before update or delete on public.johanna_one_shot_operator_dispositions
for each row execute function public.protect_johanna_one_shot_operator_disposition();

create or replace function public.protect_johanna_abandonment_one_shot_command()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    old_identity jsonb;
    new_identity jsonb;
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000',
            message = 'johanna_abandonment_one_shot_command_immutable';
    end if;

    if exists (
        select 1
        from public.johanna_one_shot_operator_dispositions disposition
        where disposition.command_id = old.id
    ) then
        raise exception using errcode = '55000',
            message = 'johanna_abandonment_one_shot_disposed_immutable';
    end if;

    old_identity := to_jsonb(old) - array[
        'status', 'chatwoot_conversation_id', 'chatwoot_message_id',
        'failure_code', 'finalized_at', 'invalid_contact_retry_count'
    ];
    new_identity := to_jsonb(new) - array[
        'status', 'chatwoot_conversation_id', 'chatwoot_message_id',
        'failure_code', 'finalized_at', 'invalid_contact_retry_count'
    ];

    if old_identity is distinct from new_identity then
        raise exception using errcode = '55000',
            message = 'johanna_abandonment_one_shot_command_immutable';
    end if;

    if current_setting('app.johanna_one_shot_invalid_contact_retry', true) = 'on'
       and old.status = 'delivery_unknown'
       and new.status = 'request_started'
       and old.failure_code = 'invalid_contact_id'
       and old.chatwoot_conversation_id is null
       and old.chatwoot_message_id is null
       and old.finalized_at is not null
       and old.invalid_contact_retry_count = 0
       and new.chatwoot_conversation_id is null
       and new.chatwoot_message_id is null
       and new.failure_code is null
       and new.finalized_at is null
       and new.invalid_contact_retry_count = 1 then
        return new;
    end if;

    if current_setting('app.johanna_one_shot_reconciliation', true) = 'on'
       and old.status = 'delivery_unknown'
       and new.status = 'accepted_by_chatwoot'
       and old.invalid_contact_retry_count = new.invalid_contact_retry_count
       and new.chatwoot_conversation_id is not null
       and new.chatwoot_conversation_id > 0
       and new.chatwoot_message_id is not null
       and new.chatwoot_message_id > 0
       and new.failure_code is null
       and new.finalized_at is not null then
        return new;
    end if;

    if old.status = 'reserved'
       and new.status = 'request_started'
       and old.invalid_contact_retry_count = new.invalid_contact_retry_count
       and new.chatwoot_conversation_id is null
       and new.chatwoot_message_id is null
       and new.failure_code is null
       and new.finalized_at is null then
        return new;
    end if;

    if old.status in ('reserved', 'request_started')
       and new.status in ('accepted_by_chatwoot', 'delivery_unknown')
       and old.invalid_contact_retry_count = new.invalid_contact_retry_count
       and new.finalized_at is not null
       and (
           (new.status = 'accepted_by_chatwoot'
               and new.chatwoot_conversation_id is not null
               and new.chatwoot_conversation_id > 0
               and new.chatwoot_message_id is not null
               and new.chatwoot_message_id > 0
               and new.failure_code is null)
           or
           (new.status = 'delivery_unknown'
               and new.failure_code is not null
               and nullif(btrim(new.failure_code), '') is not null)
       ) then
        return new;
    end if;

    raise exception using errcode = '55000',
        message = 'johanna_abandonment_one_shot_command_immutable';
end;
$function$;

create or replace function public.resolve_johanna_one_shot_unverifiable_contact_deleted(
    p_command_id uuid,
    p_operator_ref text
)
returns table (
    outcome text,
    disposition_id uuid,
    command_id uuid,
    command_status text,
    disposition_code text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    command record;
    existing public.johanna_one_shot_operator_dispositions%rowtype;
    fingerprint text;
    inserted public.johanna_one_shot_operator_dispositions%rowtype;
begin
    if p_command_id is null
       or p_operator_ref is null
       or p_operator_ref !~ '^[a-z0-9][a-z0-9:_-]{2,63}$' then
        raise exception using
            errcode = '22023',
            message = 'johanna_one_shot_operator_disposition_input_invalid';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('johanna-one-shot-operator-disposition:' || p_command_id::text, 0)
    );

    select candidate.*,
           timer.source_kind as timer_source_kind,
           timer.status as timer_status,
           timer.outcome as timer_outcome
    into command
    from public.johanna_abandonment_one_shot_commands candidate
    join public.hotmart_abandonment_reevaluations timer
      on timer.id = candidate.source_reevaluation_id
    where candidate.id = p_command_id
    for update of candidate, timer;

    if not found then
        raise exception using
            errcode = 'P0002',
            message = 'johanna_one_shot_operator_disposition_command_not_found';
    end if;

    fingerprint := encode(sha256(convert_to(concat_ws(
        chr(31), p_command_id::text, p_operator_ref,
        'unverifiable_external_contact_deleted',
        'operator_confirmed_chatwoot_contact_deleted'
    ), 'UTF8')), 'hex');

    select disposition.* into existing
    from public.johanna_one_shot_operator_dispositions disposition
    where disposition.command_id = p_command_id
    for update;

    if found then
        if existing.semantic_fingerprint is distinct from fingerprint then
            raise exception using
                errcode = '23514',
                message = 'johanna_one_shot_operator_disposition_conflict';
        end if;
        return query select 'replay'::text, existing.id, existing.command_id,
            command.status, existing.disposition_code;
        return;
    end if;

    if command.status <> 'delivery_unknown'
       or command.failure_code is distinct from 'chatwoot_http_error'
       or command.chatwoot_conversation_id is not null
       or command.chatwoot_message_id is not null
       or command.finalized_at is null
       or command.finalized_at > clock_timestamp() - interval '24 hours'
       or command.source_reevaluation_id is null
       or command.timer_source_kind <> 'precheckout_intent'
       or command.timer_status <> 'completed'
       or command.timer_outcome <> 'command_reserved'
       or command.rollout_scope <> 'johanna-precheckout-delayed-first-touch-v1'
       or command.scope_key <> 'johanna-precheckout-delayed-first-touch-production'
       or command.scope_version <> 1
       or command.runtime_generation <> 0
       or command.copy_version <> 'johanna-precheckout-delayed-first-touch-v1'
       or command.max_messages <> 1
       or command.followups_allowed <> 0 then
        raise exception using
            errcode = '55000',
            message = 'johanna_one_shot_operator_disposition_ineligible';
    end if;

    insert into public.johanna_one_shot_operator_dispositions (
        command_id, disposition_code, evidence_code, operator_ref,
        semantic_fingerprint
    ) values (
        p_command_id, 'unverifiable_external_contact_deleted',
        'operator_confirmed_chatwoot_contact_deleted', p_operator_ref,
        fingerprint
    ) returning * into inserted;

    return query select 'recorded'::text, inserted.id, inserted.command_id,
        command.status, inserted.disposition_code;
end;
$function$;

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
    v_six_bindings boolean := false;
    v_timer_binding_policy_matches boolean := false;
    v_runtime_state text;
    v_runtime_generation bigint;
    v_timer_binding_enabled boolean := false;
    v_timer_binding_generation bigint;
    v_first_touch_binding_enabled boolean := false;
    v_due_count bigint := 0;
    v_reserved_count bigint := 0;
    v_request_started_count bigint := 0;
    v_delivery_unknown_count bigint := 0;
    v_reason_code text;
begin
    if to_regclass('supabase_migrations.schema_migrations') is not null then
        execute $tracking$
            select count(*) = 6
            from supabase_migrations.schema_migrations
            where version in (
                '20260829000200', '20260829000300', '20260829000400',
                '20260829000500', '20260831000200', '20260831000300'
            )
        $tracking$ into v_tracking_complete;
    end if;

    select (
        (select count(*) = 6
         from public.johanna_precheckout_landing_offers)
        and (select count(*) = 6
             from public.hotmart_purchase_intent_scopes correlation
             join public.johanna_precheckout_landing_offers pair
               on pair.offer_ref = correlation.offer_ref
             where correlation.hotmart_product_id = '8104005'
               and correlation.tenant_ref = 'lancemos'
               and correlation.funnel_ref = 'psicologajohanna'
               and lower(correlation.purchase_intent_product_ref) = 'f106691755g'
               and correlation.max_lookback = interval '24 hours'
               and correlation.active = true)
        and exists (
            select 1
            from public.pilot_scope_versions scope
            where scope.scope_key = 'johanna-precheckout-delayed-first-touch-production'
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
              and scope.offer_code = 'explicit-six-pair-authority'
              and scope.purpose = 'cart_recovery'
              and scope.max_cohort_contacts = 1000000
              and scope.max_outbound_request_starts_total = 1000000
              and scope.max_outbound_request_starts_per_day = 10000
        )
    ) into v_scope_configured;

    select runtime.runtime_state, runtime.generation
    into v_runtime_state, v_runtime_generation
    from public.pilot_runtime_controls runtime
    where runtime.scope_key = 'johanna-precheckout-delayed-first-touch-production'
      and runtime.scope_version = 1;

    select count(*) = 6,
           coalesce(bool_and(binding.enabled), false),
           max(binding.generation),
           coalesce(bool_and(binding.precheckout_first_touch_enabled), false),
           coalesce(bool_and(
               binding.policy_key = 'johanna-precheckout-delayed-first-touch-timer'
               and binding.policy_version = 1
               and exists (
                   select 1
                   from public.followup_policy_versions policy
                   where policy.policy_key = binding.policy_key
                     and policy.version = binding.policy_version
                     and policy.status = 'published'
                     and policy.grace_period = interval '60 minutes'
               )
           ), false)
    into v_six_bindings, v_timer_binding_enabled,
         v_timer_binding_generation, v_first_touch_binding_enabled,
         v_timer_binding_policy_matches
    from public.johanna_precheckout_landing_offers pair
    join public.hotmart_abandonment_timer_policy_bindings binding
      on binding.tenant_ref = 'lancemos'
     and binding.funnel_ref = 'psicologajohanna'
     and lower(binding.product_ref) = lower('F106691755G')
     and binding.offer_ref = pair.offer_ref
    where v_scope_configured;

    v_scope_configured := v_scope_configured and v_six_bindings;

    select count(*) into v_due_count
    from public.hotmart_abandonment_reevaluations timer
    left join public.johanna_abandonment_one_shot_commands command
      on command.source_reevaluation_id = timer.id
    where timer.source_kind = 'precheckout_intent'
      and ((timer.status = 'scheduled' and timer.due_at <= clock_timestamp())
        or (timer.status = 'completed' and timer.outcome = 'command_reserved'
            and command.status in ('reserved', 'request_started')));

    select count(*) filter (where command.status = 'reserved'),
           count(*) filter (where command.status = 'request_started'),
           count(*) filter (
               where command.status = 'delivery_unknown'
                 and not exists (
                     select 1
                     from public.johanna_one_shot_operator_dispositions disposition
                     where disposition.command_id = command.id
                 )
           )
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
        when not v_timer_binding_policy_matches then 'timer_binding_policy_mismatch'
        when not v_first_touch_binding_enabled then 'first_touch_binding_disabled'
        else 'precheckout_first_touch_ready'
    end;

    return query select v_tracking_complete, v_scope_configured,
        v_runtime_state, v_runtime_generation, v_timer_binding_enabled,
        v_timer_binding_generation, v_first_touch_binding_enabled,
        v_due_count, v_reserved_count, v_request_started_count,
        v_delivery_unknown_count, v_reason_code;
end;
$function$;

revoke all on table public.johanna_one_shot_operator_dispositions from public;
revoke all on function public.protect_johanna_one_shot_operator_disposition() from public;
revoke all on function public.resolve_johanna_one_shot_unverifiable_contact_deleted(uuid, text) from public;
revoke all on function public.get_precheckout_delayed_first_touch_readiness() from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.johanna_one_shot_operator_dispositions from anon;
        revoke all on function public.protect_johanna_one_shot_operator_disposition() from anon;
        revoke all on function public.resolve_johanna_one_shot_unverifiable_contact_deleted(uuid, text) from anon;
        revoke all on function public.get_precheckout_delayed_first_touch_readiness() from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.johanna_one_shot_operator_dispositions from authenticated;
        revoke all on function public.protect_johanna_one_shot_operator_disposition() from authenticated;
        revoke all on function public.resolve_johanna_one_shot_unverifiable_contact_deleted(uuid, text) from authenticated;
        revoke all on function public.get_precheckout_delayed_first_touch_readiness() from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.johanna_one_shot_operator_dispositions from service_role;
        revoke all on function public.protect_johanna_one_shot_operator_disposition() from service_role;
        grant execute on function public.resolve_johanna_one_shot_unverifiable_contact_deleted(uuid, text) to service_role;
        grant execute on function public.get_precheckout_delayed_first_touch_readiness() to service_role;
    end if;
end;
$roles$;

commit;
