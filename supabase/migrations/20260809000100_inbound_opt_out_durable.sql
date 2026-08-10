-- Durable inbound WhatsApp opt-out for the supervised cart-recovery pilot.

begin;

create table public.contact_opt_out_events (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid references public.contacts(id) on delete restrict,
    channel text not null check (channel = 'whatsapp'),
    purpose text not null check (purpose = 'cart_recovery'),
    source text not null check (source = 'chatwoot'),
    canonical_account_id bigint not null check (canonical_account_id > 0),
    canonical_inbox_id bigint not null check (canonical_inbox_id > 0),
    canonical_conversation_id bigint not null check (canonical_conversation_id > 0),
    canonical_message_id bigint not null check (canonical_message_id > 0),
    external_user_id text not null check (external_user_id ~ '^[0-9]+$'),
    occurred_at timestamptz not null,
    normalized_rule_key text not null
        check (normalized_rule_key ~ '^[a-z0-9_]{1,64}$'),
    correlation_status text not null
        check (correlation_status in ('applied', 'unmatched', 'ambiguous')),
    projection_status text not null default 'pending'
        check (projection_status in (
            'pending', 'applied', 'retryable_failed', 'dead_letter'
        )),
    projection_attempt_count integer not null default 0
        check (projection_attempt_count >= 0),
    projection_next_attempt_at timestamptz,
    projection_error_code text,
    projection_lease_owner text,
    projection_lease_generation bigint not null default 0
        check (projection_lease_generation >= 0),
    projection_lease_expires_at timestamptz,
    created_at timestamptz not null default clock_timestamp(),
    unique (
        source,
        canonical_account_id,
        canonical_inbox_id,
        canonical_conversation_id,
        canonical_message_id
    ),
    check (
        (correlation_status = 'applied' and contact_id is not null)
        or (correlation_status <> 'applied' and contact_id is null)
    )
);

create index contact_opt_out_events_pending_projection_idx
on public.contact_opt_out_events (projection_next_attempt_at, created_at)
where projection_status in ('pending', 'retryable_failed');

create index contact_opt_out_events_conversation_stop_idx
on public.contact_opt_out_events (
    canonical_account_id,
    canonical_inbox_id,
    canonical_conversation_id,
    created_at
);

alter table public.contact_opt_out_events enable row level security;

create or replace function public.protect_authoritative_contact_opt_out()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
begin
    if old.contact_permission = 'opted_out'
       and old.lifecycle_status = 'do_not_contact'
       and (
           new.contact_permission is distinct from old.contact_permission
           or new.lifecycle_status is distinct from old.lifecycle_status
       ) then
        raise exception using
            errcode = '55000',
            message = 'authoritative_opt_out_reauthorization_required';
    end if;

    return new;
end;
$function$;

create or replace function public.protect_authoritative_opt_out_denial()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
begin
    if old.authorization_status = 'denied'
       and old.channel = 'whatsapp'
       and old.purpose = 'cart_recovery'
       and old.valid_until is null then
        raise exception using
            errcode = '55000',
            message = 'authoritative_opt_out_denial_immutable';
    end if;

    return case when tg_op = 'DELETE' then old else new end;
end;
$function$;

create trigger contacts_protect_authoritative_opt_out
before update on public.contacts
for each row execute function public.protect_authoritative_contact_opt_out();

create trigger contact_authorizations_protect_opt_out_denial
before update or delete on public.contact_authorizations
for each row execute function public.protect_authoritative_opt_out_denial();

create or replace function public.apply_chatwoot_inbound_opt_out(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_chatwoot_message_id bigint,
    p_external_user_id text,
    p_occurred_at timestamptz,
    p_rule_key text
)
returns table (
    outcome text,
    opt_out_event_id uuid,
    matched_contact_id uuid,
    affected_cases integer,
    affected_actions integer,
    affected_attempts integer
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_existing public.contact_opt_out_events%rowtype;
    v_contact_ids uuid[];
    v_contact_id uuid;
    v_correlation_status text;
    v_now timestamptz := clock_timestamp();
    v_case_ids uuid[] := '{}'::uuid[];
    v_sequence_ids uuid[] := '{}'::uuid[];
    v_action_ids uuid[] := '{}'::uuid[];
    v_attempt_ids uuid[] := '{}'::uuid[];
    v_started_attempt_count integer := 0;
    v_reconciling boolean := false;
begin
    if p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id < 1
       or p_chatwoot_message_id is null or p_chatwoot_message_id < 1
       or p_external_user_id is null
       or p_external_user_id !~ '^[0-9]+$'
       or p_occurred_at is null
       or p_rule_key is null
       or p_rule_key !~ '^[a-z0-9_]{1,64}$' then
        raise exception using
            errcode = '22023',
            message = 'invalid_chatwoot_opt_out_parameters';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':',
            'chatwoot-opt-out-user',
            p_chatwoot_account_id,
            p_external_user_id
        ),
        0
    ));

    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':',
            'chatwoot-opt-out',
            p_chatwoot_account_id,
            p_chatwoot_inbox_id,
            p_chatwoot_conversation_id,
            p_chatwoot_message_id
        ),
        0
    ));

    select event.* into v_existing
    from public.contact_opt_out_events event
    where event.source = 'chatwoot'
      and event.canonical_account_id = p_chatwoot_account_id
      and event.canonical_inbox_id = p_chatwoot_inbox_id
      and event.canonical_conversation_id = p_chatwoot_conversation_id
      and event.canonical_message_id = p_chatwoot_message_id
    for update;

    select array_agg(candidate.contact_id order by candidate.contact_id)
      into v_contact_ids
    from (
        select distinct identity.contact_id
        from public.channel_identities identity
        where identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
          and identity.external_user_id = p_external_user_id
          and identity.identity_status = 'active'
          and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
    ) candidate;

    if coalesce(cardinality(v_contact_ids), 0) = 1 then
        v_contact_id := v_contact_ids[1];
        v_correlation_status := 'applied';
    elsif coalesce(cardinality(v_contact_ids), 0) = 0 then
        v_contact_id := null;
        v_correlation_status := 'unmatched';
    else
        v_contact_id := null;
        v_correlation_status := 'ambiguous';
    end if;

    if v_existing.id is not null then
        if v_existing.channel <> 'whatsapp'
           or v_existing.purpose <> 'cart_recovery'
           or v_existing.external_user_id <> p_external_user_id
           or v_existing.occurred_at is distinct from p_occurred_at
           or v_existing.normalized_rule_key <> p_rule_key then
            outcome := 'evidence_conflict';
        elsif v_existing.contact_id is not distinct from v_contact_id
              and v_existing.correlation_status = v_correlation_status then
            outcome := 'already_applied';
        elsif v_existing.correlation_status in ('unmatched', 'ambiguous')
              and v_correlation_status = 'applied'
              and v_contact_id is not null then
            v_reconciling := true;
        else
            outcome := 'evidence_conflict';
        end if;
        if not v_reconciling then
            opt_out_event_id := v_existing.id;
            matched_contact_id := v_existing.contact_id;
            affected_cases := 0;
            affected_actions := 0;
            affected_attempts := 0;
            return next;
            return;
        end if;
    end if;

    if v_contact_id is null then
        insert into public.contact_opt_out_events (
            contact_id,
            channel,
            purpose,
            source,
            canonical_account_id,
            canonical_inbox_id,
            canonical_conversation_id,
            canonical_message_id,
            external_user_id,
            occurred_at,
            normalized_rule_key,
            correlation_status
        ) values (
            null,
            'whatsapp',
            'cart_recovery',
            'chatwoot',
            p_chatwoot_account_id,
            p_chatwoot_inbox_id,
            p_chatwoot_conversation_id,
            p_chatwoot_message_id,
            p_external_user_id,
            p_occurred_at,
            p_rule_key,
            v_correlation_status
        ) returning id into opt_out_event_id;

        outcome := case v_correlation_status
            when 'ambiguous' then 'recorded_ambiguous'
            else 'recorded_unmatched'
        end;
        matched_contact_id := null;
        affected_cases := 0;
        affected_actions := 0;
        affected_attempts := 0;
        return next;
        return;
    end if;

    -- Serialize with planning, reevaluation, reservation and request-start.
    perform 1
    from public.contacts contact
    where contact.id = v_contact_id
    for update;

    perform 1
    from public.channel_identities identity
    where identity.channel = 'whatsapp'
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.external_user_id = p_external_user_id
      and identity.contact_id = v_contact_id
      and identity.identity_status = 'active'
      and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
    for update;
    if not found then
        raise exception using
            errcode = '40001',
            message = 'chatwoot_opt_out_identity_changed';
    end if;

    perform 1
    from public.recovery_cases recovery_case
    where recovery_case.contact_id = v_contact_id
      and recovery_case.status in ('grace_period', 'active', 'paused')
    order by recovery_case.id
    for update;

    select coalesce(array_agg(recovery_case.id order by recovery_case.id), '{}'::uuid[])
      into v_case_ids
    from public.recovery_cases recovery_case
    where recovery_case.contact_id = v_contact_id
      and recovery_case.status in ('grace_period', 'active', 'paused');

    perform 1
    from public.followup_sequences sequence
    where sequence.recovery_case_id = any(v_case_ids)
      and sequence.status in ('active', 'paused')
    order by sequence.id
    for update;

    select coalesce(array_agg(sequence.id order by sequence.id), '{}'::uuid[])
      into v_sequence_ids
    from public.followup_sequences sequence
    where sequence.recovery_case_id = any(v_case_ids)
      and sequence.status in ('active', 'paused');

    perform 1
    from public.scheduled_actions action
    where action.recovery_case_id = any(v_case_ids)
      and action.status in ('pending', 'deferred', 'retryable_failed', 'delivery_unknown')
    order by action.id
    for update;

    select coalesce(array_agg(action.id order by action.id), '{}'::uuid[])
      into v_action_ids
    from public.scheduled_actions action
    where action.recovery_case_id = any(v_case_ids)
      and action.status in ('pending', 'deferred', 'retryable_failed', 'delivery_unknown');

    perform 1
    from public.followup_delivery_attempts attempt
    where attempt.action_id = any(v_action_ids)
      and attempt.phase in ('reserved', 'request_started')
    order by attempt.id
    for update;

    select coalesce(array_agg(attempt.id order by attempt.id), '{}'::uuid[])
      into v_attempt_ids
    from public.followup_delivery_attempts attempt
    where attempt.action_id = any(v_action_ids)
      and attempt.phase in ('reserved', 'request_started');

    if v_reconciling then
        update public.contact_opt_out_events
        set contact_id = v_contact_id,
            correlation_status = 'applied'
        where id = v_existing.id
        returning id into opt_out_event_id;
    else
        insert into public.contact_opt_out_events (
            contact_id,
            channel,
            purpose,
            source,
            canonical_account_id,
            canonical_inbox_id,
            canonical_conversation_id,
            canonical_message_id,
            external_user_id,
            occurred_at,
            normalized_rule_key,
            correlation_status
        ) values (
            v_contact_id,
            'whatsapp',
            'cart_recovery',
            'chatwoot',
            p_chatwoot_account_id,
            p_chatwoot_inbox_id,
            p_chatwoot_conversation_id,
            p_chatwoot_message_id,
            p_external_user_id,
            p_occurred_at,
            p_rule_key,
            'applied'
        ) returning id into opt_out_event_id;
    end if;

    update public.contacts
    set contact_permission = 'opted_out',
        lifecycle_status = 'do_not_contact',
        updated_at = v_now
    where id = v_contact_id;

    update public.contact_authorizations ca
    set valid_until = v_now
    where ca.contact_id = v_contact_id
      and ca.channel = 'whatsapp'
      and ca.purpose = 'cart_recovery'
      and ca.authorization_status = 'allowed'
      and ca.valid_from < v_now
      and (ca.valid_until is null or ca.valid_until > v_now);

    if not exists (
        select 1
        from public.contact_authorizations ca
        where ca.contact_id = v_contact_id
          and ca.channel = 'whatsapp'
          and ca.purpose = 'cart_recovery'
          and ca.authorization_status = 'denied'
          and ca.valid_from <= v_now
          and ca.valid_until is null
    ) then
        insert into public.contact_authorizations (
            contact_id,
            channel,
            purpose,
            authorization_status,
            authorization_source,
            evidence,
            valid_from
        ) values (
            v_contact_id,
            'whatsapp',
            'cart_recovery',
            'denied',
            'crm',
            jsonb_build_object(
                'source', 'chatwoot',
                'opt_out_event_id', opt_out_event_id,
                'canonical_account_id', p_chatwoot_account_id,
                'canonical_inbox_id', p_chatwoot_inbox_id,
                'canonical_conversation_id', p_chatwoot_conversation_id,
                'canonical_message_id', p_chatwoot_message_id,
                'rule_key', p_rule_key
            ),
            v_now
        );
    end if;

    update public.followup_delivery_attempts attempt
    set phase = 'completed',
        outcome = 'failed_before_request',
        reason_code = 'contact_opted_out',
        finalized_next_attempt_at = null,
        reconciliation_deadline = null,
        updated_at = v_now
    where attempt.id = any(v_attempt_ids)
      and attempt.phase = 'reserved';
    get diagnostics affected_attempts = row_count;

    update public.followup_delivery_attempts attempt
    set phase = 'completed',
        outcome = 'delivery_unknown',
        reason_code = 'contact_opted_out_after_request_started',
        finalized_next_attempt_at = null,
        reconciliation_deadline = coalesce(
            attempt.reconciliation_deadline,
            v_now + interval '15 minutes'
        ),
        updated_at = v_now
    where attempt.id = any(v_attempt_ids)
      and attempt.phase = 'request_started';
    get diagnostics v_started_attempt_count = row_count;
    affected_attempts := affected_attempts + v_started_attempt_count;

    update public.scheduled_actions action
    set status = case
            when exists (
                select 1
                from public.followup_delivery_attempts attempt
                where attempt.action_id = action.id
                  and attempt.outcome = 'delivery_unknown'
                  and attempt.reason_code = 'contact_opted_out_after_request_started'
            ) then 'delivery_unknown'
            else 'cancelled'
        end,
        terminal_reason = case
            when exists (
                select 1
                from public.followup_delivery_attempts attempt
                where attempt.action_id = action.id
                  and attempt.outcome = 'delivery_unknown'
                  and attempt.reason_code = 'contact_opted_out_after_request_started'
            ) then 'contact_opted_out_after_request_started'
            else 'contact_opted_out'
        end,
        lease_owner = null,
        lease_expires_at = null,
        next_attempt_at = null,
        updated_at = v_now
    where action.id = any(v_action_ids)
      and action.status in ('pending', 'deferred', 'retryable_failed');
    get diagnostics affected_actions = row_count;

    update public.followup_sequences sequence
    set status = 'cancelled',
        cancel_reason = 'contact_opted_out',
        cancelled_at = v_now,
        revision = revision + 1,
        updated_at = v_now
    where sequence.id = any(v_sequence_ids)
      and sequence.status in ('active', 'paused');

    update public.recovery_cases recovery_case
    set status = 'cancelled',
        next_contact_at = null,
        next_contact_reason = 'contact_opted_out',
        version = version + 1,
        updated_at = v_now
    where recovery_case.id = any(v_case_ids)
      and recovery_case.status in ('grace_period', 'active', 'paused');
    get diagnostics affected_cases = row_count;

    update public.conversations conversation
    set status = 'blocked',
        automation_status = 'disabled',
        version = version + 1,
        updated_at = v_now
    where conversation.contact_id = v_contact_id
      and conversation.commercial_context ->> 'chatwoot_conversation_id'
          = p_chatwoot_conversation_id::text;

    insert into public.conversation_events (
        conversation_id,
        recovery_case_id,
        event_type,
        actor_type,
        data
    )
    select recovery_case.conversation_id,
           recovery_case.id,
           'contact_opted_out',
           'integration',
           jsonb_build_object(
               'opt_out_event_id', opt_out_event_id,
               'canonical_message_id', p_chatwoot_message_id,
               'rule_key', p_rule_key
           )
    from public.recovery_cases recovery_case
    where recovery_case.id = any(v_case_ids)
      and recovery_case.conversation_id is not null;

    outcome := 'applied';
    matched_contact_id := v_contact_id;
    return next;
end;
$function$;

create or replace function public.reconcile_chatwoot_opt_out_stop(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_external_user_id text
)
returns table (
    outcome text,
    opt_out_event_id uuid,
    matched_contact_id uuid,
    affected_cases integer,
    affected_actions integer,
    affected_attempts integer
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_event public.contact_opt_out_events%rowtype;
begin
    if p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_chatwoot_conversation_id is null
       or p_chatwoot_conversation_id < 1
       or p_external_user_id is null
       or p_external_user_id !~ '^[0-9]+$' then
        raise exception using
            errcode = '22023',
            message = 'invalid_chatwoot_opt_out_reconciliation_parameters';
    end if;

    select event.* into v_event
    from public.contact_opt_out_events event
    where event.source = 'chatwoot'
      and event.canonical_account_id = p_chatwoot_account_id
      and event.canonical_inbox_id = p_chatwoot_inbox_id
      and (
          event.canonical_conversation_id = p_chatwoot_conversation_id
          or event.external_user_id = p_external_user_id
      )
    order by
      case event.correlation_status when 'applied' then 1 else 0 end,
      event.created_at,
      event.id
    limit 1;

    if v_event.id is null then
        raise exception using
            errcode = 'P0002',
            message = 'chatwoot_opt_out_stop_not_found';
    end if;

    return query
    select *
    from public.apply_chatwoot_inbound_opt_out(
        v_event.canonical_account_id,
        v_event.canonical_inbox_id,
        v_event.canonical_conversation_id,
        v_event.canonical_message_id,
        v_event.external_user_id,
        v_event.occurred_at,
        v_event.normalized_rule_key
    );
end;
$function$;

create or replace function public.has_chatwoot_opt_out_stop(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_external_user_id text
)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $function$
    select exists (
        select 1
        from public.contact_opt_out_events event
        where event.source = 'chatwoot'
          and event.canonical_account_id = p_chatwoot_account_id
          and event.canonical_inbox_id = p_chatwoot_inbox_id
          and (
              event.canonical_conversation_id = p_chatwoot_conversation_id
              or event.external_user_id = p_external_user_id
          )
    );
$function$;

create or replace function public.claim_chatwoot_opt_out_projections(
    p_worker_id text,
    p_now timestamptz,
    p_lease_duration interval,
    p_batch_size integer
)
returns table (
    opt_out_event_id uuid,
    chatwoot_conversation_id bigint,
    lease_generation bigint
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
begin
    if p_worker_id is null or btrim(p_worker_id) = ''
       or p_now is null
       or p_lease_duration is null or p_lease_duration <= interval '0 seconds'
       or p_batch_size is null or p_batch_size < 1 or p_batch_size > 100 then
        raise exception using
            errcode = '22023',
            message = 'invalid_chatwoot_opt_out_projection_claim_parameters';
    end if;

    return query
    with candidates as (
        select event.id
        from public.contact_opt_out_events event
        where event.projection_status in ('pending', 'retryable_failed')
          and (event.projection_next_attempt_at is null
               or event.projection_next_attempt_at <= p_now)
          and (event.projection_lease_expires_at is null
               or event.projection_lease_expires_at <= p_now)
        order by event.created_at, event.id
        for update skip locked
        limit p_batch_size
    ), claimed as (
        update public.contact_opt_out_events event
        set projection_lease_owner = p_worker_id,
            projection_lease_generation = event.projection_lease_generation + 1,
            projection_lease_expires_at = p_now + p_lease_duration
        from candidates
        where event.id = candidates.id
        returning event.id,
                  event.canonical_conversation_id,
                  event.projection_lease_generation
    )
    select claimed.id,
           claimed.canonical_conversation_id,
           claimed.projection_lease_generation
    from claimed;
end;
$function$;

create or replace function public.finalize_chatwoot_opt_out_projection(
    p_opt_out_event_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_applied boolean,
    p_error_code text,
    p_max_attempts integer,
    p_now timestamptz
)
returns setof public.contact_opt_out_events
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_event public.contact_opt_out_events%rowtype;
    v_attempt_count integer;
begin
    if p_opt_out_event_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_applied is null
       or p_max_attempts is null or p_max_attempts < 1 or p_max_attempts > 100
       or p_now is null
       or (not p_applied and (p_error_code is null or btrim(p_error_code) = '')) then
        raise exception using
            errcode = '22023',
            message = 'invalid_chatwoot_opt_out_projection_finalization_parameters';
    end if;

    select event.* into strict v_event
    from public.contact_opt_out_events event
    where event.id = p_opt_out_event_id
    for update;

    if v_event.projection_status = 'applied' and p_applied then
        return next v_event;
        return;
    end if;
    if v_event.projection_lease_owner is distinct from p_worker_id
       or v_event.projection_lease_generation is distinct from p_lease_generation
       or v_event.projection_lease_expires_at is null
       or v_event.projection_lease_expires_at <= p_now then
        raise exception using
            errcode = 'P0002',
            message = 'chatwoot_opt_out_projection_lease_not_found';
    end if;

    v_attempt_count := v_event.projection_attempt_count + 1;
    return query
    update public.contact_opt_out_events event
    set projection_status = case
            when p_applied then 'applied'
            when v_attempt_count >= p_max_attempts then 'dead_letter'
            else 'retryable_failed'
        end,
        projection_attempt_count = v_attempt_count,
        projection_next_attempt_at = case
            when p_applied or v_attempt_count >= p_max_attempts then null
            else p_now + make_interval(
                secs => least(300, (5 * power(2, v_attempt_count - 1))::integer)
            )
        end,
        projection_error_code = case when p_applied then null else p_error_code end,
        projection_lease_owner = null,
        projection_lease_expires_at = null
    where event.id = p_opt_out_event_id
    returning event.*;
end;
$function$;

alter function public.mark_followup_request_started(
    uuid, uuid, text, bigint, timestamptz
) rename to _mark_followup_request_started_without_opt_out_guard;

create or replace function public.mark_followup_request_started(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns setof public.followup_delivery_attempts
language plpgsql
security definer
set search_path = public, pg_temp
as $request_start$
declare
    v_account_id bigint;
    v_external_user_id text;
begin
    select substring(identity.account_id from '^chatwoot:([0-9]+)$')::bigint,
           identity.external_user_id
      into v_account_id, v_external_user_id
    from public.scheduled_actions action
    join public.recovery_cases recovery_case
      on recovery_case.id = action.recovery_case_id
    join public.channel_identities identity
      on identity.id = recovery_case.selected_channel_identity_id
    where action.id = p_action_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active';

    if v_account_id is not null and v_external_user_id is not null then
        perform pg_advisory_xact_lock(hashtextextended(
            concat_ws(
                ':', 'chatwoot-opt-out-user', v_account_id, v_external_user_id
            ),
            0
        ));

        if exists (
            select 1
            from public.contact_opt_out_events event
            where event.source = 'chatwoot'
              and event.channel = 'whatsapp'
              and event.canonical_account_id = v_account_id
              and event.external_user_id = v_external_user_id
              and event.correlation_status in (
                  'applied', 'unmatched', 'ambiguous', 'evidence_conflict'
              )
        ) then
            raise exception using
                errcode = '55000',
                message = 'pending_chatwoot_opt_out_stop';
        end if;
    end if;

    return query
    select *
    from public._mark_followup_request_started_without_opt_out_guard(
        p_action_id,
        p_attempt_id,
        p_worker_id,
        p_lease_generation,
        p_now
    );
end;
$request_start$;

do $drop_reconciliation_retry_shape$
declare
    v_constraint_name text;
begin
    select constraint_row.conname into strict v_constraint_name
    from pg_constraint constraint_row
    where constraint_row.conrelid = 'public.followup_delivery_attempts'::regclass
      and constraint_row.contype = 'c'
      and pg_get_constraintdef(constraint_row.oid) like
          '%reconciliation_next_attempt_at IS NOT NULL%';
    execute format(
        'alter table public.followup_delivery_attempts drop constraint %I',
        v_constraint_name
    );
end;
$drop_reconciliation_retry_shape$;

alter table public.followup_delivery_attempts
add constraint followup_delivery_attempts_reconciliation_retry_shape_check
check (
    (
        reconciliation_resolution = 'not_applied'
        and (
            reconciliation_next_attempt_at is not null
            or reason_code = 'contact_opted_out_not_applied'
        )
    )
    or (
        reconciliation_resolution is distinct from 'not_applied'
        and reconciliation_next_attempt_at is null
    )
);

create or replace function public._finalize_opted_out_followup_not_applied(
    p_action_id uuid,
    p_attempt_id uuid,
    p_lease_generation bigint,
    p_now timestamptz
)
returns setof public.scheduled_actions
language plpgsql
security definer
set search_path = public, pg_temp
as $opt_out_not_applied$
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
begin
    select action.* into strict v_action
    from public.scheduled_actions action
    where action.id = p_action_id;

    perform 1
    from public.contacts contact
    join public.recovery_cases recovery_case on recovery_case.contact_id = contact.id
    where recovery_case.id = v_action.recovery_case_id
    for update of contact;

    select recovery_case.* into strict v_case
    from public.recovery_cases recovery_case
    where recovery_case.id = v_action.recovery_case_id
    for update;

    perform 1
    from public.followup_sequences sequence
    where sequence.id = v_action.followup_sequence_id
    for update;

    select action.* into strict v_action
    from public.scheduled_actions action
    where action.id = p_action_id
    for update;

    select attempt.* into strict v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = p_attempt_id
      and attempt.action_id = p_action_id
      and attempt.lease_generation = p_lease_generation
    for update;

    if v_attempt.reconciliation_resolution = 'not_applied'
       and v_attempt.reason_code = 'contact_opted_out_not_applied' then
        return next v_action;
        return;
    end if;

    if v_attempt.phase <> 'completed'
       or v_attempt.outcome <> 'delivery_unknown'
       or v_attempt.reconciliation_resolution is not null
       or not exists (
           select 1
           from public.contacts contact
           where contact.id = v_case.contact_id
             and contact.contact_permission = 'opted_out'
             and contact.lifecycle_status = 'do_not_contact'
       ) then
        raise exception using
            errcode = '55000',
            message = 'opted_out_delivery_not_pending_reconciliation';
    end if;

    update public.followup_delivery_attempts
    set outcome = 'rejected',
        reconciliation_resolution = 'not_applied',
        reconciliation_next_attempt_at = null,
        reconciled_at = p_now,
        reason_code = 'contact_opted_out_not_applied',
        reconciliation_deadline = null,
        finalized_next_attempt_at = null
    where id = p_attempt_id;

    update public.scheduled_actions
    set status = 'cancelled',
        terminal_reason = 'contact_opted_out_not_applied',
        next_attempt_at = null,
        lease_owner = null,
        lease_expires_at = null,
        updated_at = p_now
    where id = p_action_id
    returning * into strict v_action;

    insert into public.conversation_events (
        recovery_case_id,
        event_type,
        actor_type,
        related_action_id,
        data
    ) values (
        v_action.recovery_case_id,
        'followup_delivery_reconciled',
        'system',
        p_action_id,
        jsonb_build_object(
            'resolution', 'not_applied',
            'reason_code', 'contact_opted_out_not_applied',
            'attempt_id', p_attempt_id,
            'lease_generation', p_lease_generation
        )
    );

    return next v_action;
end;
$opt_out_not_applied$;

alter function public.reconcile_followup_delivery_attempt(
    uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz
) rename to _reconcile_followup_delivery_attempt_without_opt_out;

create or replace function public.reconcile_followup_delivery_attempt(
    p_action_id uuid,
    p_attempt_id uuid,
    p_lease_generation bigint,
    p_resolution text,
    p_remote_message_id text,
    p_accepted_message_id uuid,
    p_next_attempt_at timestamptz,
    p_reason_code text,
    p_now timestamptz
)
returns setof public.scheduled_actions
language plpgsql
security definer
set search_path = public, pg_temp
as $reconcile$
begin
    if p_resolution = 'not_applied'
       and exists (
           select 1
           from public.followup_delivery_attempts attempt
           where attempt.id = p_attempt_id
             and attempt.action_id = p_action_id
             and attempt.lease_generation = p_lease_generation
             and (
                 (
                     attempt.outcome = 'delivery_unknown'
                     and attempt.reason_code = 'contact_opted_out_after_request_started'
                 )
                 or (
                     attempt.outcome = 'rejected'
                     and attempt.reason_code = 'contact_opted_out_not_applied'
                 )
             )
       ) then
        return query
        select * from public._finalize_opted_out_followup_not_applied(
            p_action_id, p_attempt_id, p_lease_generation, p_now
        );
        return;
    end if;

    return query
    select * from public._reconcile_followup_delivery_attempt_without_opt_out(
        p_action_id,
        p_attempt_id,
        p_lease_generation,
        p_resolution,
        p_remote_message_id,
        p_accepted_message_id,
        p_next_attempt_at,
        p_reason_code,
        p_now
    );
end;
$reconcile$;

create or replace function public.finalize_followup_delivery_attempt(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_outcome text,
    p_remote_message_id text,
    p_accepted_message_id uuid,
    p_reason_code text,
    p_next_attempt_at timestamptz,
    p_reconciliation_deadline timestamptz,
    p_now timestamptz
)
returns setof public.scheduled_actions
language plpgsql
security definer
set search_path = public, pg_temp
as $finalize$
begin
    if p_outcome = 'accepted_by_chatwoot'
       or p_accepted_message_id is not null then
        raise exception using
            errcode = '55000',
            message = 'canonical_acceptance_required';
    end if;

    if p_outcome = 'rejected'
       and exists (
           select 1
           from public.followup_delivery_attempts attempt
           where attempt.id = p_attempt_id
             and attempt.action_id = p_action_id
             and attempt.lease_generation = p_lease_generation
             and (
                 (
                     attempt.outcome = 'delivery_unknown'
                     and attempt.reason_code = 'contact_opted_out_after_request_started'
                 )
                 or (
                     attempt.outcome = 'rejected'
                     and attempt.reason_code = 'contact_opted_out_not_applied'
                 )
             )
       ) then
        return query
        select * from public._finalize_opted_out_followup_not_applied(
            p_action_id, p_attempt_id, p_lease_generation, p_now
        );
        return;
    end if;

    return query
    select * from public._finalize_followup_delivery_attempt(
        p_action_id, p_attempt_id, p_worker_id, p_lease_generation,
        p_outcome, p_remote_message_id, null, p_reason_code,
        p_next_attempt_at, p_reconciliation_deadline, p_now
    );
end;
$finalize$;

alter function public.reserve_followup_delivery_attempt(
    uuid, text, bigint, bigint, bigint, text, text, timestamptz
) security definer;
alter function public._mark_followup_request_started_without_opt_out_guard(
    uuid, uuid, text, bigint, timestamptz
) security definer;
alter function public._finalize_followup_delivery_attempt(
    uuid, uuid, text, bigint, text, text, uuid, text, timestamptz,
    timestamptz, timestamptz
) security definer;
alter function public.record_and_finalize_followup_acceptance(
    uuid, uuid, text, bigint, text, text, text, timestamptz
) security definer;
alter function public._reconcile_followup_delivery_attempt_without_opt_out(
    uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz
) security definer;

do $attempt_privileges$
begin
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke insert, update, delete on public.followup_delivery_attempts
        from service_role;
        grant select on public.followup_delivery_attempts to service_role;
        revoke execute on function public._mark_followup_request_started_without_opt_out_guard(
            uuid, uuid, text, bigint, timestamptz
        ) from service_role;
        revoke execute on function public._finalize_followup_delivery_attempt(
            uuid, uuid, text, bigint, text, text, uuid, text, timestamptz,
            timestamptz, timestamptz
        ) from service_role;
        revoke execute on function public._reconcile_followup_delivery_attempt_without_opt_out(
            uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz
        ) from service_role;
        revoke execute on function public._finalize_opted_out_followup_not_applied(
            uuid, uuid, bigint, timestamptz
        ) from service_role;
    end if;
end;
$attempt_privileges$;

revoke all on public.contact_opt_out_events from public;
revoke execute on function public.mark_followup_request_started(
    uuid, uuid, text, bigint, timestamptz
) from public;
revoke execute on function public.reconcile_followup_delivery_attempt(
    uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz
) from public;
revoke execute on function public._mark_followup_request_started_without_opt_out_guard(
    uuid, uuid, text, bigint, timestamptz
) from public;
revoke execute on function public._reconcile_followup_delivery_attempt_without_opt_out(
    uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz
) from public;
revoke execute on function public._finalize_opted_out_followup_not_applied(
    uuid, uuid, bigint, timestamptz
) from public;
revoke execute on function public.apply_chatwoot_inbound_opt_out(
    bigint, bigint, bigint, bigint, text, timestamptz, text
) from public;
revoke execute on function public.has_chatwoot_opt_out_stop(bigint, bigint, bigint, text)
from public;
revoke execute on function public.reconcile_chatwoot_opt_out_stop(bigint, bigint, bigint, text)
from public;
revoke execute on function public.claim_chatwoot_opt_out_projections(
    text, timestamptz, interval, integer
) from public;
revoke execute on function public.finalize_chatwoot_opt_out_projection(
    uuid, text, bigint, boolean, text, integer, timestamptz
) from public;
revoke execute on function public.protect_authoritative_contact_opt_out()
from public;
revoke execute on function public.protect_authoritative_opt_out_denial()
from public;

do $privileges$
declare
    v_function regprocedure;
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on public.contact_opt_out_events from anon';
        execute 'revoke execute on function public.apply_chatwoot_inbound_opt_out(bigint, bigint, bigint, bigint, text, timestamptz, text) from anon';
        execute 'revoke execute on function public.has_chatwoot_opt_out_stop(bigint, bigint, bigint, text) from anon';
        execute 'revoke execute on function public.reconcile_chatwoot_opt_out_stop(bigint, bigint, bigint, text) from anon';
        execute 'revoke execute on function public.claim_chatwoot_opt_out_projections(text, timestamptz, interval, integer) from anon';
        execute 'revoke execute on function public.finalize_chatwoot_opt_out_projection(uuid, text, bigint, boolean, text, integer, timestamptz) from anon';
        execute 'revoke execute on function public.protect_authoritative_contact_opt_out() from anon';
        execute 'revoke execute on function public.protect_authoritative_opt_out_denial() from anon';
        execute 'revoke execute on function public.mark_followup_request_started(uuid, uuid, text, bigint, timestamptz) from anon';
        execute 'revoke execute on function public.reconcile_followup_delivery_attempt(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) from anon';
        execute 'revoke execute on function public._mark_followup_request_started_without_opt_out_guard(uuid, uuid, text, bigint, timestamptz) from anon';
        execute 'revoke execute on function public._reconcile_followup_delivery_attempt_without_opt_out(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) from anon';
        execute 'revoke execute on function public._finalize_opted_out_followup_not_applied(uuid, uuid, bigint, timestamptz) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on public.contact_opt_out_events from authenticated';
        execute 'revoke execute on function public.apply_chatwoot_inbound_opt_out(bigint, bigint, bigint, bigint, text, timestamptz, text) from authenticated';
        execute 'revoke execute on function public.has_chatwoot_opt_out_stop(bigint, bigint, bigint, text) from authenticated';
        execute 'revoke execute on function public.reconcile_chatwoot_opt_out_stop(bigint, bigint, bigint, text) from authenticated';
        execute 'revoke execute on function public.claim_chatwoot_opt_out_projections(text, timestamptz, interval, integer) from authenticated';
        execute 'revoke execute on function public.finalize_chatwoot_opt_out_projection(uuid, text, bigint, boolean, text, integer, timestamptz) from authenticated';
        execute 'revoke execute on function public.protect_authoritative_contact_opt_out() from authenticated';
        execute 'revoke execute on function public.protect_authoritative_opt_out_denial() from authenticated';
        execute 'revoke execute on function public.mark_followup_request_started(uuid, uuid, text, bigint, timestamptz) from authenticated';
        execute 'revoke execute on function public.reconcile_followup_delivery_attempt(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) from authenticated';
        execute 'revoke execute on function public._mark_followup_request_started_without_opt_out_guard(uuid, uuid, text, bigint, timestamptz) from authenticated';
        execute 'revoke execute on function public._reconcile_followup_delivery_attempt_without_opt_out(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) from authenticated';
        execute 'revoke execute on function public._finalize_opted_out_followup_not_applied(uuid, uuid, bigint, timestamptz) from authenticated';
    end if;
    for v_function in
        select procedure.oid::regprocedure
        from pg_proc as procedure
        where procedure.pronamespace = 'public'::regnamespace
          and procedure.prosecdef
    loop
        if exists (select 1 from pg_roles where rolname = 'anon') then
            execute format(
                'revoke execute on function %s from anon',
                v_function
            );
        end if;
        if exists (select 1 from pg_roles where rolname = 'authenticated') then
            execute format(
                'revoke execute on function %s from authenticated',
                v_function
            );
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke all on public.contact_opt_out_events from service_role';
        execute 'grant select on public.contact_opt_out_events to service_role';
        execute 'revoke execute on function public.protect_authoritative_contact_opt_out() from service_role';
        execute 'revoke execute on function public.protect_authoritative_opt_out_denial() from service_role';
        execute 'grant execute on function public.apply_chatwoot_inbound_opt_out(bigint, bigint, bigint, bigint, text, timestamptz, text) to service_role';
        execute 'grant execute on function public.has_chatwoot_opt_out_stop(bigint, bigint, bigint, text) to service_role';
        execute 'grant execute on function public.reconcile_chatwoot_opt_out_stop(bigint, bigint, bigint, text) to service_role';
        execute 'grant execute on function public.claim_chatwoot_opt_out_projections(text, timestamptz, interval, integer) to service_role';
        execute 'grant execute on function public.finalize_chatwoot_opt_out_projection(uuid, text, bigint, boolean, text, integer, timestamptz) to service_role';
        execute 'grant execute on function public.mark_followup_request_started(uuid, uuid, text, bigint, timestamptz) to service_role';
        execute 'grant execute on function public.reconcile_followup_delivery_attempt(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) to service_role';
        execute 'grant execute on function public.finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) to service_role';
    end if;
end;
$privileges$;

commit;
