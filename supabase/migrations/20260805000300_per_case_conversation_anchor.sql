-- Migration: per-case conversation authority (ADR-0008)
--
-- Chatwoot opens a NEW conversation per session. A recurring buyer who abandons
-- more than once (or abandons two different products at the same time) yields
-- several Chatwoot conversations under ONE WhatsApp channel identity. The
-- original engine treated channel_identities.external_conversation_id as the
-- single reply-authority anchor, welding one identity to one conversation
-- forever. That (1) made a new case reevaluate reply-authority against a prior
-- case's stale conversation and (2) made record_and_finalize reject the new
-- conversation with channel_identity_conversation_mismatch (HTTP 400).
--
-- Fix (authority moves from the identity to the case):
--   1. get_followup_chatwoot_context now returns the CASE's own conversation
--      (recovery_cases.conversation_id) as the authority to check, not the
--      identity anchor. A case with no conversation yet yields null.
--   2. reevaluate_followup_action gates the clean first-contact path on the
--      case having no conversation, and validates the checked conversation
--      against the case's conversation.
--   3. record_and_finalize_followup_acceptance drops the identity-anchor
--      mismatch guard. external_conversation_id becomes a last-write-wins
--      denormalized pointer (kept only to satisfy the unique index); it is
--      never read for authority. The per-case invariant is still enforced by
--      case_conversation_mismatch / sequence_conversation_mismatch, so a case
--      can never jump between conversations.
--
-- This removes the shared-anchor hijack between concurrent cases of the same
-- buyer entirely, rather than merely detecting it. Additive: three function
-- bodies replaced, no schema/DDL changes.

create or replace function public.get_followup_chatwoot_context(
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns table (
    action_id uuid,
    action_type text,
    chatwoot_account_id text,
    external_conversation_id text,
    expected_inbox_id bigint,
    anchor_external_message_id text
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
begin
    if p_action_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_followup_fence';
    end if;

    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;

    if not (
        v_action.lease_owner = p_worker_id
        and v_action.lease_generation = p_lease_generation
        and v_action.lease_expires_at > p_now
        and v_action.expires_at > p_now
        and v_action.status in ('pending', 'deferred', 'retryable_failed')
    ) then
        raise exception using errcode = '55000', message = 'current_action_lease_not_found';
    end if;

    -- ADR-0008: reply authority is the CASE's own conversation, never the
    -- identity-level anchor. A recurring buyer opens a fresh Chatwoot
    -- conversation per case; reading rc.conversation_id keeps each case's
    -- no-reply check scoped to its own conversation even when several cases
    -- share one WhatsApp identity. A case with no conversation yet (first
    -- contact) yields null, which the worker treats as "nothing to check".
    return query
    select
        v_action.id,
        v_action.action_type,
        ci.account_id,
        case_conv.commercial_context ->> 'chatwoot_conversation_id',
        case
            when ci.metadata ->> 'inbox_id' ~ '^[1-9][0-9]*$'
            then (ci.metadata ->> 'inbox_id')::bigint
            else null
        end,
        m.external_message_id
    from public.recovery_cases rc
    left join public.channel_identities ci
      on ci.id = rc.selected_channel_identity_id
     and ci.contact_id = rc.contact_id
     and ci.channel = 'whatsapp'
     and ci.identity_status = 'active'
    left join public.conversations case_conv
      on case_conv.id = rc.conversation_id
     and case_conv.contact_id = rc.contact_id
     and case_conv.channel_identity_id = ci.id
    left join public.messages m
      on m.id = v_action.anchor_subject_internal_id
    where rc.id = v_action.recovery_case_id;
end;
$function$;

create or replace function public.reevaluate_followup_action(
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz,
    p_chatwoot_checked boolean default false,
    p_chatwoot_conversation_id text default null,
    p_chatwoot_checkpoint_message_id text default null,
    p_chatwoot_checkpoint_at timestamptz default null,
    p_chatwoot_status text default null,
    p_chatwoot_can_reply boolean default null,
    p_chatwoot_anchor_found boolean default null,
    p_chatwoot_automation_paused boolean default null,
    p_chatwoot_inbound_after_anchor boolean default null,
    p_chatwoot_human_activity_after_anchor boolean default null
)
returns table (
    action_id uuid,
    decision text,
    reason_code text,
    case_version bigint,
    sequence_revision bigint
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_case public.recovery_cases%rowtype;
    v_sequence public.followup_sequences%rowtype;
    v_contact public.contacts%rowtype;
    v_policy public.followup_policy_versions%rowtype;
    v_step jsonb;
    v_business_window_open boolean;
    v_next_business_at timestamptz;
    v_decision text;
    v_reason text;
    v_replay_data jsonb;
begin
    if p_action_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_followup_fence';
    end if;

    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;

    -- Global order for this aggregate: contact -> case -> sequence -> action.
    select c.* into strict v_contact
    from public.contacts c
    join public.recovery_cases rc on rc.contact_id = c.id
    where rc.id = v_action.recovery_case_id
    for update of c;

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

    -- Replay after a committed response was lost.
    select ce.data into v_replay_data
    from public.conversation_events ce
    where ce.related_action_id = p_action_id
      and ce.event_type = 'followup_action_reevaluated'
      and ce.data ->> 'decision' <> 'execute'
      and ce.data ->> 'worker_id' = p_worker_id
      and (ce.data ->> 'lease_generation')::bigint = p_lease_generation
    order by ce.created_at desc
    limit 1;

    if found then
        return query select
            p_action_id,
            v_replay_data ->> 'decision',
            v_replay_data ->> 'reason_code',
            (v_replay_data ->> 'case_version')::bigint,
            (v_replay_data ->> 'sequence_revision')::bigint;
        return;
    end if;

    if not (
        v_action.lease_owner = p_worker_id
        and v_action.lease_generation = p_lease_generation
        and v_action.lease_expires_at > p_now
        and v_action.status in ('pending', 'deferred', 'retryable_failed')
    ) then
        raise exception using errcode = 'P0002', message = 'current_action_lease_not_found';
    end if;

    select fpv.* into strict v_policy
    from public.followup_policy_versions fpv
    where fpv.policy_key = v_action.policy_key
      and fpv.version = v_action.policy_version;

    select policy_step into v_step
    from jsonb_array_elements(v_policy.steps) as policy_step
    where policy_step ->> 'step_key' = v_action.step_key
    limit 1;

    select exists (
        select 1
        from jsonb_array_elements(v_policy.business_windows) as business_window
        where business_window -> 'days' @> jsonb_build_array(
                  extract(isodow from p_now at time zone v_policy.timezone)::integer
              )
          and business_window ->> 'start' ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
          and business_window ->> 'end' ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
          and to_char(p_now at time zone v_policy.timezone, 'HH24:MI')
              >= business_window ->> 'start'
          and to_char(p_now at time zone v_policy.timezone, 'HH24:MI')
              <= business_window ->> 'end'
    ) into v_business_window_open;

    if not v_business_window_open then
        select min(
            (
                ((p_now at time zone v_policy.timezone)::date + day_offset)
                + (business_window ->> 'start')::time
            ) at time zone v_policy.timezone
        ) into v_next_business_at
        from generate_series(0, 7) as offsets(day_offset)
        cross join jsonb_array_elements(v_policy.business_windows) as business_window
        where business_window ->> 'start' ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
          and business_window ->> 'end' ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
          and business_window -> 'days' @> jsonb_build_array(
                  extract(
                      isodow from
                      (p_now at time zone v_policy.timezone)::date + day_offset
                  )::integer
              )
          and (
                ((p_now at time zone v_policy.timezone)::date + day_offset)
                + (business_window ->> 'start')::time
              ) at time zone v_policy.timezone > p_now;
    end if;

    if v_action.expires_at <= p_now then
        update public.scheduled_actions
        set status = 'expired', terminal_reason = 'reevaluation_expired',
            lease_owner = null, lease_expires_at = null
        where id = p_action_id;
        update public.followup_sequences
        set status = 'completed', completion_reason = 'expired',
            completed_at = p_now, revision = revision + 1
        where id = v_sequence.id and status = 'active';
        update public.recovery_cases
        set status = 'expired', closed_at = p_now, version = version + 1
        where id = v_case.id and status in ('grace_period', 'active');
        select rc.* into strict v_case from public.recovery_cases rc where rc.id = v_case.id;
        select fs.* into strict v_sequence from public.followup_sequences fs where fs.id = v_sequence.id;
        insert into public.conversation_events (
            recovery_case_id, event_type, actor_type, related_action_id, data
        ) values (
            v_case.id, 'followup_action_reevaluated', 'system', p_action_id,
            jsonb_build_object('decision', 'expire', 'reason_code', 'expired',
                               'worker_id', p_worker_id,
                               'case_version', v_case.version,
                               'sequence_revision', v_sequence.revision,
                               'lease_generation', p_lease_generation)
        );
        return query select p_action_id, 'expire'::text, 'expired'::text,
                            v_case.version, v_sequence.revision;
        return;
    end if;

    if v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or v_contact.lifecycle_status = 'do_not_contact' then
        v_decision := 'cancel';
        v_reason := 'contact_blocked';
    elsif v_case.purchase_event_id is not null or v_case.status = 'won' then
        v_decision := 'cancel';
        v_reason := 'purchase_detected';
    elsif v_case.status not in ('grace_period', 'active')
       or v_sequence.status <> 'active'
       or v_case.version <> v_action.expected_case_version then
        v_decision := 'pause';
        v_reason := 'authoritative_state_changed';
    elsif exists (
        select 1 from public.conversations c
        where c.id = v_case.conversation_id
          and c.contact_id = v_case.contact_id
          and c.channel_identity_id = v_case.selected_channel_identity_id
          and (c.human_takeover or c.status = 'paused_human'
               or c.automation_status in ('paused', 'disabled', 'restricted'))
    ) then
        v_decision := 'pause';
        v_reason := 'human_takeover_active';
    elsif v_case.identity_resolution_status <> 'resolved'
       or not exists (
        select 1 from public.channel_identities ci
        where ci.id = v_case.selected_channel_identity_id
          and ci.contact_id = v_case.contact_id
          and ci.channel = 'whatsapp'
          and ci.identity_status = 'active'
    ) then
        v_decision := 'escalate';
        v_reason := 'identity_not_authorized';
    elsif exists (
        select 1 from public.contact_authorizations ca
        where ca.contact_id = v_case.contact_id
          and ca.channel = 'whatsapp'
          and ca.purpose = 'cart_recovery'
          and ca.valid_from <= p_now
          and (ca.valid_until is null or ca.valid_until > p_now)
          and ca.authorization_status in ('denied', 'restricted')
    ) then
        v_decision := 'cancel';
        v_reason := 'contact_authorization_denied';
    elsif not exists (
        select 1 from public.contact_authorizations ca
        where ca.contact_id = v_case.contact_id
          and ca.channel = 'whatsapp'
          and ca.purpose = 'cart_recovery'
          and ca.valid_from <= p_now
          and (ca.valid_until is null or ca.valid_until > p_now)
          and ca.authorization_status = 'allowed'
    ) then
        v_decision := 'escalate';
        v_reason := 'contact_authorization_unknown';
    elsif v_policy.status <> 'published' or v_step is null then
        v_decision := 'escalate';
        v_reason := 'policy_step_invalid';
    elsif v_step ->> 'mode' <> 'freeform' then
        v_decision := 'escalate';
        v_reason := 'channel_mode_unsupported';
    elsif v_sequence.automatic_messages_accepted >= v_policy.max_automatic_messages then
        v_decision := 'cancel';
        v_reason := 'automatic_message_limit_reached';
    elsif not v_business_window_open
       and (v_next_business_at is null or v_next_business_at >= v_action.expires_at) then
        v_decision := 'escalate';
        v_reason := 'no_business_window_before_expiry';
    elsif not v_business_window_open then
        v_decision := 'defer';
        v_reason := 'business_window_closed';
    elsif v_action.action_type = 'reconcile_delivery' then
        v_decision := 'escalate';
        v_reason := 'reconciliation_requires_dedicated_worker';
    elsif v_action.action_type = 'no_reply_review'
       and (
           v_action.conversation_id is null
           or not exists (
               select 1
               from public.messages m
               where m.id = v_action.anchor_subject_internal_id
                 and m.conversation_id = v_action.conversation_id
                 and m.direction = 'outbound'
                 and m.external_message_id is not null
                 and btrim(m.external_message_id) <> ''
           )
       ) then
        v_decision := 'escalate';
        v_reason := 'no_reply_anchor_invalid';
    elsif v_action.action_type = 'first_contact_review'
       and not p_chatwoot_checked
       and v_case.conversation_id is null then
        v_decision := 'execute';
        v_reason := 'eligible_for_execution';
    elsif not p_chatwoot_checked then
        v_decision := 'escalate';
        v_reason := 'chatwoot_authority_unavailable';
    elsif p_chatwoot_conversation_id is null
       or p_chatwoot_checkpoint_message_id is null
       or p_chatwoot_checkpoint_at is null
       or p_chatwoot_status is null
       or p_chatwoot_can_reply is null
       or p_chatwoot_anchor_found is null
       or p_chatwoot_automation_paused is null
       or p_chatwoot_inbound_after_anchor is null
       or p_chatwoot_human_activity_after_anchor is null
       or not exists (
           select 1 from public.conversations c
           where c.id = v_case.conversation_id
             and c.contact_id = v_case.contact_id
             and c.channel_identity_id = v_case.selected_channel_identity_id
             and c.commercial_context ->> 'chatwoot_conversation_id' = p_chatwoot_conversation_id
       ) then
        raise exception using errcode = '22023', message = 'invalid_chatwoot_authority_evidence';
    elsif v_action.action_type = 'no_reply_review'
       and not p_chatwoot_anchor_found then
        v_decision := 'escalate';
        v_reason := 'chatwoot_anchor_not_found';
    elsif p_chatwoot_inbound_after_anchor then
        v_decision := 'cancel';
        v_reason := 'prospect_replied';
    elsif p_chatwoot_automation_paused
       or p_chatwoot_human_activity_after_anchor
       or not p_chatwoot_can_reply
       or p_chatwoot_status in ('snoozed', 'resolved') then
        v_decision := 'pause';
        v_reason := 'chatwoot_conversation_blocked';
    else
        v_decision := 'execute';
        v_reason := 'eligible_for_execution';
    end if;

    if v_decision = 'defer' then
        update public.scheduled_actions
        set status = 'deferred',
            due_at = v_next_business_at,
            next_attempt_at = null,
            terminal_reason = null,
            lease_owner = null,
            lease_expires_at = null
        where id = p_action_id;
    elsif v_decision <> 'execute' then
        update public.scheduled_actions
        set status = 'cancelled', terminal_reason = v_reason,
            lease_owner = null, lease_expires_at = null
        where id = p_action_id;

        if v_decision in ('pause', 'escalate') then
            update public.followup_sequences
            set status = 'paused', revision = revision + 1
            where id = v_sequence.id and status = 'active';
            update public.recovery_cases
            set status = case when v_decision = 'pause' then 'paused' else 'escalated' end,
                version = version + 1
            where id = v_case.id and status in ('grace_period', 'active');
        elsif v_reason = 'prospect_replied' then
            update public.followup_sequences
            set status = 'completed', completion_reason = v_reason,
                completed_at = p_now, revision = revision + 1
            where id = v_sequence.id and status = 'active';
            -- The commercial case stays open; only automated no-reply work ends.
        elsif v_reason = 'automatic_message_limit_reached' then
            update public.followup_sequences
            set status = 'completed', completion_reason = v_reason,
                completed_at = p_now, revision = revision + 1
            where id = v_sequence.id and status = 'active';
            update public.recovery_cases
            set status = 'sequence_exhausted', closed_at = p_now,
                version = version + 1
            where id = v_case.id and status in ('grace_period', 'active');
        elsif v_reason in (
            'purchase_detected', 'contact_blocked', 'contact_authorization_denied'
        ) then
            update public.followup_sequences
            set status = 'completed', completion_reason = v_reason,
                completed_at = p_now, revision = revision + 1
            where id = v_sequence.id and status = 'active';
            update public.recovery_cases
            set status = case when v_reason = 'purchase_detected' then 'won' else 'cancelled' end,
                won_at = case when v_reason = 'purchase_detected' then p_now else won_at end,
                closed_at = p_now, version = version + 1
            where id = v_case.id and status in ('grace_period', 'active');
        end if;

        select rc.* into strict v_case
        from public.recovery_cases rc
        where rc.id = v_case.id;
        select fs.* into strict v_sequence
        from public.followup_sequences fs
        where fs.id = v_sequence.id;
    end if;

    insert into public.conversation_events (
        recovery_case_id, event_type, actor_type, related_action_id, data
    ) values (
        v_case.id, 'followup_action_reevaluated', 'system', p_action_id,
        jsonb_build_object('decision', v_decision, 'reason_code', v_reason,
                           'worker_id', p_worker_id,
                           'policy_key', v_action.policy_key,
                           'policy_version', v_action.policy_version,
                           'case_version', v_case.version,
                           'sequence_revision', v_sequence.revision,
                           'lease_generation', p_lease_generation,
                           'chatwoot_checked', p_chatwoot_checked,
                           'chatwoot_conversation_id', p_chatwoot_conversation_id,
                           'chatwoot_checkpoint_message_id', p_chatwoot_checkpoint_message_id,
                           'chatwoot_checkpoint_at', p_chatwoot_checkpoint_at,
                           'chatwoot_status', p_chatwoot_status,
                           'chatwoot_can_reply', p_chatwoot_can_reply,
                           'chatwoot_anchor_found', p_chatwoot_anchor_found,
                           'chatwoot_automation_paused', p_chatwoot_automation_paused,
                           'chatwoot_inbound_after_anchor', p_chatwoot_inbound_after_anchor,
                           'chatwoot_human_activity_after_anchor', p_chatwoot_human_activity_after_anchor)
    );

    return query select p_action_id, v_decision, v_reason,
                        v_case.version, v_sequence.revision;
end;
$function$;

create or replace function public.record_and_finalize_followup_acceptance(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_external_conversation_id text,
    p_remote_message_id text,
    p_message_content text,
    p_now timestamptz
)
returns setof public.scheduled_actions
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_sequence public.followup_sequences%rowtype;
    v_contact public.contacts%rowtype;
    v_identity public.channel_identities%rowtype;
    v_conversation public.conversations%rowtype;
    v_message public.messages%rowtype;
    v_reconciling boolean := false;
begin
    if p_action_id is null
       or p_attempt_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation <= 0
       or p_external_conversation_id is null or btrim(p_external_conversation_id) = ''
       or p_remote_message_id is null or btrim(p_remote_message_id) = ''
       or p_message_content is null or btrim(p_message_content) = ''
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_acceptance_parameters';
    end if;

    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;

    select c.* into strict v_contact
    from public.contacts c
    join public.recovery_cases rc on rc.contact_id = c.id
    where rc.id = v_action.recovery_case_id
    for update of c;

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

    if v_attempt.phase = 'completed' then
        if v_attempt.outcome = 'accepted_by_chatwoot' then
            select m.* into strict v_message
            from public.messages m
            where m.id = v_attempt.accepted_message_id;
            select c.* into strict v_conversation
            from public.conversations c
            where c.id = v_message.conversation_id;
            if v_message.external_message_id is distinct from p_remote_message_id
               or v_message.content is distinct from p_message_content
               or v_message.direction <> 'outbound'
               or v_message.actor_type <> 'ai_agent'
               or v_message.delivery_status not in ('accepted', 'sent', 'delivered', 'read')
               or v_message.semantic_metadata ->> 'attempt_id' is distinct from p_attempt_id::text
               or v_message.semantic_metadata ->> 'action_id' is distinct from p_action_id::text
               or v_conversation.commercial_context ->> 'chatwoot_conversation_id'
                  is distinct from p_external_conversation_id then
                raise exception using errcode = '22000', message = 'delivery_attempt_already_finalized_differently';
            end if;
            return query
            select * from public._finalize_followup_delivery_attempt(
                p_action_id, p_attempt_id, p_worker_id, p_lease_generation,
                'accepted_by_chatwoot', p_remote_message_id, v_message.id,
                'accepted_by_chatwoot', null, null, p_now
            );
            return;
        elsif v_attempt.outcome = 'delivery_unknown'
              and v_attempt.reconciliation_resolution is null then
            if v_attempt.reconciliation_deadline is null
               or p_now > v_attempt.reconciliation_deadline then
                raise exception using
                    errcode = '55000',
                    message = 'reconciliation_window_expired';
            end if;
            update public.followup_delivery_attempts
            set phase = 'request_started',
                outcome = null,
                remote_message_id = null,
                accepted_message_id = null,
                accepted_at = null,
                reason_code = null
            where id = p_attempt_id
            returning * into strict v_attempt;
            v_reconciling := true;
        else
            raise exception using errcode = '22000', message = 'delivery_attempt_already_finalized_differently';
        end if;
    end if;

    if v_attempt.phase <> 'request_started' then
        raise exception using errcode = 'P0002', message = 'delivery_request_not_started';
    end if;

    select ci.* into strict v_identity
    from public.channel_identities ci
    where ci.id = v_case.selected_channel_identity_id
      and ci.contact_id = v_case.contact_id
      and ci.channel = 'whatsapp'
      and ci.identity_status = 'active'
    for update;

    -- ADR-0008: the identity-level conversation anchor is no longer the reply
    -- authority (get_followup_chatwoot_context and reevaluate_followup_action
    -- now read the case's own conversation). The per-case invariant is enforced
    -- below by case_conversation_mismatch. We keep external_conversation_id as a
    -- last-write-wins denormalized pointer to the identity's most recent
    -- conversation so the unique index stays satisfied; it is never read for
    -- authority, so a recurring buyer's second case may advance it freely.
    if v_identity.external_conversation_id is distinct from p_external_conversation_id then
        update public.channel_identities
        set external_conversation_id = p_external_conversation_id,
            updated_at = p_now
        where id = v_identity.id
        returning * into strict v_identity;
    end if;

    select c.* into v_conversation
    from public.conversations c
    where c.channel_identity_id = v_identity.id
      and c.commercial_context ->> 'chatwoot_conversation_id' = p_external_conversation_id
    for update;

    if not found then
        insert into public.conversations (
            contact_id, channel_identity_id, status, automation_status,
            human_takeover, commercial_context
        ) values (
            v_case.contact_id, v_identity.id, 'active', 'enabled', false,
            jsonb_build_object('chatwoot_conversation_id', p_external_conversation_id)
        )
        on conflict do nothing
        returning * into v_conversation;

        if not found then
            select c.* into strict v_conversation
            from public.conversations c
            where c.channel_identity_id = v_identity.id
              and c.commercial_context ->> 'chatwoot_conversation_id' =
                  p_external_conversation_id
            for update;
        end if;
    end if;

    if v_case.conversation_id is not null
       and v_case.conversation_id is distinct from v_conversation.id then
        raise exception using errcode = '22000', message = 'case_conversation_mismatch';
    end if;
    if v_sequence.conversation_id is not null
       and v_sequence.conversation_id is distinct from v_conversation.id then
        raise exception using errcode = '22000', message = 'sequence_conversation_mismatch';
    end if;

    select m.* into v_message
    from public.messages m
    where m.external_message_id = p_remote_message_id
    for update;

    if not found then
        insert into public.messages (
            conversation_id, external_message_id, direction, actor_type,
            message_type, content, delivery_status, expects_reply,
            reply_expectation_type, is_followup, followup_step,
            semantic_metadata, occurred_at
        ) values (
            v_conversation.id, p_remote_message_id, 'outbound', 'ai_agent',
            'followup', p_message_content, 'accepted', true,
            'freeform', v_action.action_type = 'no_reply_review',
            v_sequence.current_step + 1,
            jsonb_build_object(
                'attempt_id', p_attempt_id,
                'action_id', p_action_id,
                'strategy', 'durable_followup'
            ),
            p_now
        )
        on conflict do nothing
        returning * into v_message;

        if not found then
            select m.* into strict v_message
            from public.messages m
            where m.external_message_id = p_remote_message_id
            for update;
        end if;
    end if;

    if v_message.conversation_id is distinct from v_conversation.id
       or v_message.direction <> 'outbound'
       or v_message.actor_type <> 'ai_agent'
       or v_message.delivery_status not in ('accepted', 'sent', 'delivered', 'read')
       or v_message.content is distinct from p_message_content
       or v_message.semantic_metadata ->> 'attempt_id' is distinct from p_attempt_id::text
       or v_message.semantic_metadata ->> 'action_id' is distinct from p_action_id::text then
        raise exception using errcode = '22000', message = 'canonical_message_mismatch';
    end if;

    update public.conversations
    set last_message_id = v_message.id,
        last_message_direction = 'outbound',
        last_outbound_at = p_now,
        version = version + 1
    where id = v_conversation.id;

    update public.recovery_cases
    set conversation_id = v_conversation.id
    where id = v_case.id;

    update public.followup_sequences
    set conversation_id = v_conversation.id
    where id = v_sequence.id;

    select * into strict v_action
    from public._finalize_followup_delivery_attempt(
        p_action_id, p_attempt_id, p_worker_id, p_lease_generation,
        'accepted_by_chatwoot', p_remote_message_id, v_message.id,
        'accepted_by_chatwoot', null, null, p_now
    );

    if v_reconciling then
        update public.followup_delivery_attempts
        set reconciliation_resolution = 'accepted_by_chatwoot',
            reconciled_at = p_now
        where id = p_attempt_id;
    end if;

    return next v_action;
end;
$function$;
