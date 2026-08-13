-- Corrige la semántica de follow-up para que cada delay sea un offset absoluto
-- desde la primera aceptación outbound durable de la secuencia, no una demora
-- encadenada desde la aceptación anterior. No publica policies ni activa runtime.

begin;

create or replace function public.validate_followup_policy_step_offsets()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_step jsonb;
    v_delay interval;
begin
    for v_step in
        select step
        from jsonb_array_elements(new.steps) as step
        where step ? 'delay'
    loop
        if jsonb_typeof(v_step -> 'delay') <> 'string'
           or nullif(btrim(v_step ->> 'delay'), '') is null then
            raise exception using
                errcode = '22023',
                message = 'invalid_policy_step_offset';
        end if;
        begin
            v_delay := (v_step ->> 'delay')::interval;
        exception
            when invalid_datetime_format then
                raise exception using
                    errcode = '22023',
                    message = 'invalid_policy_step_offset';
        end;
        if v_delay < interval '0 seconds' then
            raise exception using
                errcode = '22023',
                message = 'negative_policy_step_offset';
        end if;
    end loop;
    return new;
end;
$function$;

do $existing_policy_preflight$
declare
    v_policy public.followup_policy_versions%rowtype;
    v_step jsonb;
    v_delay interval;
begin
    for v_policy in
        select policy.*
        from public.followup_policy_versions policy
        order by policy.policy_key, policy.version
    loop
        for v_step in
            select step
            from jsonb_array_elements(v_policy.steps) as step
            where step ? 'delay'
        loop
            if jsonb_typeof(v_step -> 'delay') <> 'string'
               or nullif(btrim(v_step ->> 'delay'), '') is null then
                raise exception using
                    errcode = '22023',
                    message = 'existing_policy_step_offset_invalid',
                    detail = format('%s/%s', v_policy.policy_key, v_policy.version);
            end if;
            begin
                v_delay := (v_step ->> 'delay')::interval;
            exception
                when invalid_datetime_format then
                    raise exception using
                        errcode = '22023',
                        message = 'existing_policy_step_offset_invalid',
                        detail = format('%s/%s', v_policy.policy_key, v_policy.version);
            end;
            if v_delay < interval '0 seconds' then
                raise exception using
                    errcode = '22023',
                    message = 'existing_policy_step_offset_negative',
                    detail = format('%s/%s', v_policy.policy_key, v_policy.version);
            end if;
        end loop;
    end loop;
end;
$existing_policy_preflight$;

drop trigger if exists followup_policy_step_offsets_validate
on public.followup_policy_versions;

create trigger followup_policy_step_offsets_validate
before insert or update of steps on public.followup_policy_versions
for each row execute function public.validate_followup_policy_step_offsets();

create or replace function public._finalize_followup_delivery_attempt(
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
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_sequence public.followup_sequences%rowtype;
    v_policy public.followup_policy_versions%rowtype;
    v_case_version bigint;
    v_next_step jsonb;
    v_next_delay interval;
    v_sequence_started_at timestamptz;
    v_next_due_at timestamptz;
    v_completion_reason text;
    v_next_action_id uuid;
    v_has_current_lease boolean;
    v_authoritative_current boolean;
    v_from_status text;
begin
    if p_outcome not in (
        'accepted_by_chatwoot', 'rejected', 'failed_before_request',
        'delivery_unknown'
    ) then
        raise exception using errcode = '22023', message = 'invalid_delivery_outcome';
    end if;
    if p_outcome = 'accepted_by_chatwoot'
       and (p_remote_message_id is null or btrim(p_remote_message_id) = '') then
        raise exception using errcode = '22023', message = 'remote_message_id_required';
    end if;
    if p_outcome = 'accepted_by_chatwoot' and p_accepted_message_id is null then
        raise exception using errcode = '22023', message = 'accepted_message_id_required';
    end if;
    if p_outcome = 'delivery_unknown'
       and (
           p_reconciliation_deadline is null
           or p_reconciliation_deadline <= p_now
       ) then
        raise exception using errcode = '22023', message = 'future_reconciliation_deadline_required';
    end if;

    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;

    select rc.* into strict v_case
    from public.recovery_cases rc
    where rc.id = v_action.recovery_case_id
    for update;

    select fs.* into strict v_sequence
    from public.followup_sequences fs
    where fs.id = v_action.followup_sequence_id
    for update;

    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id
    for update;

    select fda.* into strict v_attempt
    from public.followup_delivery_attempts fda
    where fda.id = p_attempt_id
      and fda.action_id = p_action_id
      and fda.lease_generation = p_lease_generation
    for update;

    v_from_status := v_action.status;

    if v_attempt.phase = 'completed' then
        if v_attempt.outcome is distinct from p_outcome
           or v_attempt.remote_message_id is distinct from p_remote_message_id
           or v_attempt.reason_code is distinct from p_reason_code
           or (
               p_outcome = 'accepted_by_chatwoot'
               and v_attempt.accepted_message_id is distinct from p_accepted_message_id
           )
           or (
               p_outcome in ('failed_before_request', 'rejected')
               and v_attempt.finalized_next_attempt_at is distinct from p_next_attempt_at
           )
           or (
               p_outcome = 'delivery_unknown'
               and v_attempt.reconciliation_deadline is distinct from p_reconciliation_deadline
           ) then
            raise exception using
                errcode = '22000',
                message = 'delivery_attempt_already_finalized_differently';
        end if;

        return next v_action;
        return;
    end if;

    v_has_current_lease := (
        v_action.lease_owner = p_worker_id
        and v_action.lease_generation = p_lease_generation
        and v_action.lease_expires_at > p_now
        and v_action.status in ('pending', 'deferred', 'retryable_failed')
    );
    v_authoritative_current := (
        v_case.version = v_attempt.expected_case_version
        and v_sequence.revision = v_attempt.expected_sequence_revision
        and v_case.status in ('grace_period', 'active')
        and v_sequence.status = 'active'
        and v_action.expires_at > p_now
        and v_action.status in (
            'pending', 'deferred', 'retryable_failed', 'delivery_unknown'
        )
        and exists (
            select 1 from public.contacts c
            where c.id = v_case.contact_id
              and c.contact_permission not in ('opted_out', 'blocked', 'restricted')
              and c.lifecycle_status <> 'do_not_contact'
        )
        and exists (
            select 1 from public.contact_authorizations ca
            where ca.contact_id = v_case.contact_id
              and ca.channel = 'whatsapp'
              and ca.purpose = 'cart_recovery'
              and ca.authorization_status = 'allowed'
              and ca.valid_from <= p_now
              and (ca.valid_until is null or ca.valid_until > p_now)
        )
        and not exists (
            select 1 from public.contact_authorizations ca
            where ca.contact_id = v_case.contact_id
              and ca.channel = 'whatsapp'
              and ca.purpose = 'cart_recovery'
              and ca.authorization_status in ('denied', 'restricted')
              and ca.valid_from <= p_now
              and (ca.valid_until is null or ca.valid_until > p_now)
        )
        and not exists (
            select 1 from public.conversations c
            where c.id = coalesce(v_case.conversation_id, v_sequence.conversation_id)
              and (
                  c.human_takeover
                  or c.automation_status in ('paused', 'disabled', 'restricted', 'error')
                  or c.status in ('snoozed', 'paused_human', 'completed', 'closed', 'blocked')
                  or c.last_inbound_at > v_attempt.request_started_at
              )
        )
    );

    if not v_has_current_lease
       and (v_attempt.phase <> 'request_started' or p_outcome = 'failed_before_request') then
        raise exception using errcode = 'P0002', message = 'current_action_lease_not_found';
    end if;

    update public.followup_delivery_attempts
    set phase = 'completed',
        outcome = p_outcome,
        remote_message_id = p_remote_message_id,
        accepted_message_id = case
            when p_outcome = 'accepted_by_chatwoot' then p_accepted_message_id
            else null
        end,
        accepted_at = case
            when p_outcome = 'accepted_by_chatwoot' then p_now
            else null
        end,
        reason_code = p_reason_code,
        finalized_next_attempt_at = case
            when p_outcome in ('failed_before_request', 'rejected') then p_next_attempt_at
            else null
        end,
        reconciliation_deadline = case
            when p_outcome = 'delivery_unknown' then p_reconciliation_deadline
            else null
        end
    where id = p_attempt_id;

    if p_outcome = 'accepted_by_chatwoot' then
        update public.scheduled_actions
        set status = 'accepted_by_chatwoot',
            executed_at = p_now,
            terminal_reason = 'accepted_by_chatwoot',
            next_attempt_at = null,
            lease_owner = null,
            lease_expires_at = null
        where id = p_action_id
        returning * into v_action;

        if not v_authoritative_current then
            update public.scheduled_actions
            set terminal_reason = 'accepted_by_chatwoot:authoritative_state_changed_after_reservation'
            where id = p_action_id
            returning * into v_action;
        else
            update public.followup_sequences
        set current_step = current_step + 1,
            automatic_messages_accepted = automatic_messages_accepted + 1,
            revision = revision + 1
        where id = v_action.followup_sequence_id
        returning * into v_sequence;

        update public.recovery_cases
        set status = case when status = 'grace_period' then 'active' else status end,
            version = version + 1
        where id = v_action.recovery_case_id
        returning version into v_case_version;

        select * into strict v_policy
        from public.followup_policy_versions
        where policy_key = v_action.policy_key
          and version = v_action.policy_version;

        v_next_step := v_policy.steps -> v_sequence.current_step;

        if v_sequence.automatic_messages_accepted < v_sequence.max_attempts
           and v_next_step is not null then
            if coalesce(v_next_step ->> 'step_key', '') = ''
               or coalesce(v_next_step ->> 'delay', '') = '' then
                raise exception using
                    errcode = '22023',
                    message = 'invalid_next_policy_step';
            end if;

            v_next_delay := (v_next_step ->> 'delay')::interval;
            if v_next_delay < interval '0 seconds' then
                raise exception using
                    errcode = '22023',
                    message = 'negative_policy_step_offset';
            end if;

            select min(attempt.accepted_at) into strict v_sequence_started_at
            from public.followup_delivery_attempts attempt
            join public.scheduled_actions prior_action
              on prior_action.id = attempt.action_id
            where prior_action.followup_sequence_id = v_sequence.id
              and attempt.outcome = 'accepted_by_chatwoot'
              and attempt.accepted_at is not null;

            if v_sequence_started_at is null then
                raise exception using
                    errcode = '55000',
                    message = 'followup_sequence_start_acceptance_missing';
            end if;
            v_next_due_at := v_sequence_started_at + v_next_delay;
        end if;

        if v_sequence.automatic_messages_accepted < v_sequence.max_attempts
           and v_next_step is not null
           and v_next_due_at < v_action.expires_at then
            insert into public.scheduled_actions (
                followup_sequence_id,
                recovery_case_id,
                action_type,
                status,
                due_at,
                expires_at,
                expected_case_version,
                max_execution_retries,
                idempotency_key,
                policy_key,
                policy_version,
                step_key,
                conversation_id,
                anchor_type,
                anchor_subject_internal_id,
                anchor_observed_at,
                anchor_checkpoint
            ) values (
                v_sequence.id,
                v_action.recovery_case_id,
                'no_reply_review',
                'pending',
                v_next_due_at,
                v_action.expires_at,
                v_case_version,
                v_action.max_execution_retries,
                'cart_recovery:' || (v_next_step ->> 'step_key') || ':' || v_sequence.id::text,
                v_action.policy_key,
                v_action.policy_version,
                v_next_step ->> 'step_key',
                (select m.conversation_id from public.messages m where m.id = p_accepted_message_id),
                'accepted_outbound_message',
                p_accepted_message_id,
                p_now,
                jsonb_build_object(
                    'attempt_id', p_attempt_id,
                    'remote_message_id', p_remote_message_id
                )
            )
            returning id into v_next_action_id;
        else
            v_completion_reason := case
                when v_next_step is not null
                     and v_sequence.automatic_messages_accepted < v_sequence.max_attempts
                    then 'next_step_outside_expiration'
                else 'policy_exhausted'
            end;

            update public.followup_sequences
            set status = 'completed',
                completion_reason = v_completion_reason,
                completed_at = p_now,
                revision = revision + 1
            where id = v_sequence.id;

            update public.recovery_cases
            set status = 'sequence_exhausted',
                closed_at = p_now,
                version = version + 1
            where id = v_action.recovery_case_id;
        end if;
        end if;

    elsif p_outcome = 'delivery_unknown' and not v_authoritative_current then
        update public.scheduled_actions
        set terminal_reason = concat_ws(
                ':',
                nullif(terminal_reason, ''),
                'delivery_unknown_after_authoritative_state_change'
            ),
            lease_owner = null,
            lease_expires_at = null
        where id = p_action_id
        returning * into v_action;

    elsif p_outcome = 'delivery_unknown' then
        update public.scheduled_actions
        set status = 'delivery_unknown',
            terminal_reason = p_reason_code,
            next_attempt_at = null,
            lease_owner = null,
            lease_expires_at = null
        where id = p_action_id
        returning * into v_action;

    elsif p_outcome = 'rejected' and not v_authoritative_current then
        update public.scheduled_actions
        set terminal_reason = concat_ws(
                ':',
                nullif(terminal_reason, ''),
                'rejected_after_authoritative_state_change'
            ),
            lease_owner = null,
            lease_expires_at = null
        where id = p_action_id
        returning * into v_action;

    elsif p_outcome in ('failed_before_request', 'rejected') then
        if p_next_attempt_at is not null
           and v_action.execution_attempt_count <= v_action.max_execution_retries then
            if p_next_attempt_at < v_action.expires_at then
                update public.scheduled_actions
                set status = 'retryable_failed',
                    next_attempt_at = p_next_attempt_at,
                    terminal_reason = null,
                    error_code = p_reason_code,
                    lease_owner = null,
                    lease_expires_at = null
                where id = p_action_id
                returning * into v_action;
            else
                update public.scheduled_actions
                set status = 'expired',
                    next_attempt_at = null,
                    terminal_reason = 'retry_beyond_expiration',
                    error_code = p_reason_code,
                    lease_owner = null,
                    lease_expires_at = null
                where id = p_action_id
                returning * into v_action;
            end if;
        else
            update public.scheduled_actions
            set status = 'permanent_failed',
                next_attempt_at = null,
                terminal_reason = p_reason_code,
                error_code = p_reason_code,
                lease_owner = null,
                lease_expires_at = null
            where id = p_action_id
            returning * into v_action;
        end if;
    end if;

    insert into public.conversation_events (
        recovery_case_id,
        event_type,
        actor_type,
        related_action_id,
        data
    ) values (
        v_action.recovery_case_id,
        'followup_delivery_finalized',
        'system',
        p_action_id,
        jsonb_build_object(
            'policy_key', v_action.policy_key,
            'policy_version', v_action.policy_version,
            'from_status', v_from_status,
            'to_status', v_action.status,
            'reason_code', p_reason_code,
            'attempt_id', p_attempt_id,
            'next_action_id', v_next_action_id,
            'lease_generation', p_lease_generation
        )
    );

    return next v_action;
end;
$function$;

revoke execute on function public._finalize_followup_delivery_attempt(
    uuid, uuid, text, bigint, text, text, uuid, text, timestamptz,
    timestamptz, timestamptz
) from public;

revoke execute on function public.validate_followup_policy_step_offsets()
from public;

do $privileges$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke execute on function public._finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) from anon';
        execute 'revoke execute on function public.validate_followup_policy_step_offsets() from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke execute on function public._finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) from authenticated';
        execute 'revoke execute on function public.validate_followup_policy_step_offsets() from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke execute on function public._finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) from service_role';
        execute 'revoke execute on function public.validate_followup_policy_step_offsets() from service_role';
    end if;
end;
$privileges$;

commit;
