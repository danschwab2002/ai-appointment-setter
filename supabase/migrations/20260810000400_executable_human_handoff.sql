-- Durable executable human handoff for the supervised Lancemos pilot.

begin;

create table public.human_handoff_projection_policies (
    id uuid primary key default gen_random_uuid(),
    policy_key text not null
        check (policy_key ~ '^[a-z0-9_-]{1,100}$'),
    policy_version integer not null check (policy_version > 0),
    scope_key text not null,
    scope_version integer not null check (scope_version > 0),
    expected_team_id bigint not null check (expected_team_id > 0),
    note_template_key text not null
        check (note_template_key ~ '^[a-z0-9_-]{1,100}$'),
    note_template_version integer not null
        check (note_template_version > 0),
    private_note_body text not null
        check (char_length(private_note_body) between 1 and 1800),
    active boolean not null default false,
    created_at timestamptz not null default clock_timestamp(),
    unique (policy_key, policy_version),
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version)
        on delete restrict
);

create table public.human_handoff_requests (
    id uuid primary key default gen_random_uuid(),
    recovery_case_id uuid not null
        references public.recovery_cases(id) on delete restrict,
    conversation_id uuid not null
        references public.conversations(id) on delete restrict,
    source_action_id uuid
        references public.scheduled_actions(id) on delete restrict,
    source_attempt_id uuid
        references public.followup_delivery_attempts(id) on delete restrict,
    command_key text not null unique
        check (command_key ~ '^[a-z0-9:_-]{1,200}$'),
    primary_reason_code text not null
        check (primary_reason_code in (
            'explicit_human_request',
            'commercial_exception',
            'policy_requires_human'
        )),
    requested_by text not null
        check (requested_by in ('system', 'agent', 'operator')),
    projection_policy_key text not null
        check (projection_policy_key ~ '^[a-z0-9_-]{1,100}$'),
    projection_policy_version integer not null
        check (projection_policy_version > 0),
    scope_key text not null,
    scope_version integer not null check (scope_version > 0),
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    external_conversation_id bigint not null check (external_conversation_id > 0),
    expected_team_id bigint not null check (expected_team_id > 0),
    note_template_key text not null
        check (note_template_key ~ '^[a-z0-9_-]{1,100}$'),
    note_template_version integer not null
        check (note_template_version > 0),
    private_note_body text not null
        check (char_length(private_note_body) between 1 and 1800),
    status text not null default 'requested'
        check (status in (
            'requested',
            'projected',
            'projection_failed',
            'dead_letter'
        )),
    last_error_code text,
    created_at timestamptz not null default clock_timestamp(),
    projected_at timestamptz,
    updated_at timestamptz not null default clock_timestamp(),
    check (
        (status = 'projected' and projected_at is not null)
        or (status <> 'projected' and projected_at is null)
    )
);

create unique index human_handoff_requests_one_live_per_case_idx
on public.human_handoff_requests (recovery_case_id)
where status in ('requested', 'projection_failed');

create table public.human_handoff_request_evidence (
    id uuid primary key default gen_random_uuid(),
    handoff_request_id uuid not null
        references public.human_handoff_requests(id) on delete restrict,
    reason_code text not null
        check (reason_code in (
            'explicit_human_request',
            'commercial_exception',
            'policy_requires_human'
        )),
    source_action_id uuid
        references public.scheduled_actions(id) on delete restrict,
    source_attempt_id uuid
        references public.followup_delivery_attempts(id) on delete restrict,
    requested_by text not null
        check (requested_by in ('system', 'agent', 'operator')),
    command_key text not null unique
        check (command_key ~ '^[a-z0-9:_-]{1,200}$'),
    created_at timestamptz not null default clock_timestamp()
);

create table public.human_handoff_projection_effects (
    id uuid primary key default gen_random_uuid(),
    handoff_request_id uuid not null
        references public.human_handoff_requests(id) on delete restrict,
    effect_kind text not null
        check (effect_kind in ('assignment', 'private_note')),
    effect_status text not null default 'pending'
        check (effect_status in (
            'pending',
            'applied',
            'retryable_failed',
            'delivery_unknown',
            'dead_letter',
            'conflict'
        )),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    next_attempt_at timestamptz,
    last_error_code text,
    lease_owner text,
    lease_generation bigint not null default 0 check (lease_generation >= 0),
    lease_expires_at timestamptz,
    applied_at timestamptz,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    unique (handoff_request_id, effect_kind),
    check (
        (effect_status = 'applied' and applied_at is not null)
        or (effect_status <> 'applied' and applied_at is null)
    ),
    check (
        (lease_owner is null and lease_expires_at is null)
        or (lease_owner is not null and lease_expires_at is not null)
    )
);

create index human_handoff_projection_effects_claim_idx
on public.human_handoff_projection_effects (next_attempt_at, created_at)
where effect_status in ('pending', 'retryable_failed', 'delivery_unknown');

create or replace function public.protect_human_handoff_policy_version()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000',
            message = 'human_handoff_policy_version_is_immutable';
    end if;
    if old.active
       and not new.active
       and new.id = old.id
       and new.policy_key = old.policy_key
       and new.policy_version = old.policy_version
       and new.scope_key = old.scope_key
       and new.scope_version = old.scope_version
       and new.expected_team_id = old.expected_team_id
       and new.note_template_key = old.note_template_key
       and new.note_template_version = old.note_template_version
       and new.private_note_body = old.private_note_body
       and new.created_at = old.created_at then
        return new;
    end if;
    raise exception using errcode = '55000',
        message = 'human_handoff_policy_version_is_immutable';
end;
$function$;

create trigger human_handoff_projection_policies_immutable
before update or delete on public.human_handoff_projection_policies
for each row execute function public.protect_human_handoff_policy_version();

create or replace function public.protect_human_handoff_request_identity()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE'
       or new.id is distinct from old.id
       or new.recovery_case_id is distinct from old.recovery_case_id
       or new.conversation_id is distinct from old.conversation_id
       or new.source_action_id is distinct from old.source_action_id
       or new.source_attempt_id is distinct from old.source_attempt_id
       or new.command_key is distinct from old.command_key
       or new.primary_reason_code is distinct from old.primary_reason_code
       or new.requested_by is distinct from old.requested_by
       or new.projection_policy_key is distinct from old.projection_policy_key
       or new.projection_policy_version is distinct from old.projection_policy_version
       or new.scope_key is distinct from old.scope_key
       or new.scope_version is distinct from old.scope_version
       or new.chatwoot_account_id is distinct from old.chatwoot_account_id
       or new.chatwoot_inbox_id is distinct from old.chatwoot_inbox_id
       or new.external_conversation_id is distinct from old.external_conversation_id
       or new.expected_team_id is distinct from old.expected_team_id
       or new.note_template_key is distinct from old.note_template_key
       or new.note_template_version is distinct from old.note_template_version
       or new.private_note_body is distinct from old.private_note_body
       or new.created_at is distinct from old.created_at then
        raise exception using errcode = '55000',
            message = 'human_handoff_request_identity_is_immutable';
    end if;
    return new;
end;
$function$;

create trigger human_handoff_requests_protect_identity
before update or delete on public.human_handoff_requests
for each row execute function public.protect_human_handoff_request_identity();

create or replace function public.reject_human_handoff_evidence_mutation()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $function$
begin
    raise exception using errcode = '55000',
        message = 'human_handoff_evidence_is_append_only';
end;
$function$;

create trigger human_handoff_request_evidence_append_only
before update or delete on public.human_handoff_request_evidence
for each row execute function public.reject_human_handoff_evidence_mutation();

create or replace function public.protect_handoff_projection_effect_identity()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE'
       or new.id is distinct from old.id
       or new.handoff_request_id is distinct from old.handoff_request_id
       or new.effect_kind is distinct from old.effect_kind
       or new.created_at is distinct from old.created_at then
        raise exception using errcode = '55000',
            message = 'human_handoff_projection_effect_identity_is_immutable';
    end if;
    return new;
end;
$function$;

create trigger human_handoff_projection_effects_protect_identity
before update or delete on public.human_handoff_projection_effects
for each row execute function public.protect_handoff_projection_effect_identity();

create or replace function public.request_human_handoff(
    p_recovery_case_id uuid,
    p_command_key text,
    p_reason_code text,
    p_requested_by text,
    p_projection_policy_key text,
    p_projection_policy_version integer,
    p_source_action_id uuid default null,
    p_source_attempt_id uuid default null,
    p_worker_id text default null,
    p_lease_generation bigint default null,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    handoff_request_id uuid,
    affected_actions integer,
    affected_attempts integer
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_contact_id uuid;
    v_case public.recovery_cases%rowtype;
    v_policy public.human_handoff_projection_policies%rowtype;
    v_existing public.human_handoff_requests%rowtype;
    v_evidence public.human_handoff_request_evidence%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
    v_binding public.pilot_recovery_case_bindings%rowtype;
    v_identity public.channel_identities%rowtype;
    v_conversation public.conversations%rowtype;
    v_sequence_ids uuid[] := '{}'::uuid[];
    v_action_ids uuid[] := '{}'::uuid[];
    v_attempt_ids uuid[] := '{}'::uuid[];
    v_started_attempts integer := 0;
    v_now timestamptz;
begin
    if p_recovery_case_id is null
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_reason_code not in (
           'explicit_human_request',
           'commercial_exception',
           'policy_requires_human'
       )
       or p_requested_by not in ('system', 'agent', 'operator')
       or (p_requested_by = 'agent' and p_source_attempt_id is null)
       or p_projection_policy_key is null
       or p_projection_policy_version is null
       or p_projection_policy_version < 1
       or p_now is null
       or ((p_source_action_id is null) <> (p_source_attempt_id is null))
       or ((p_source_attempt_id is null) <> (p_worker_id is null))
       or ((p_source_attempt_id is null) <> (p_lease_generation is null)) then
        raise exception using
            errcode = '22023',
            message = 'invalid_human_handoff_parameters';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('human_handoff_command:' || p_command_key, 0)
    );

    select request.* into v_existing
    from public.human_handoff_requests request
    where request.command_key = p_command_key;

    if v_existing.id is not null then
        if v_existing.recovery_case_id <> p_recovery_case_id
           or v_existing.primary_reason_code <> p_reason_code
           or v_existing.requested_by <> p_requested_by
           or v_existing.source_action_id is distinct from p_source_action_id
           or v_existing.source_attempt_id is distinct from p_source_attempt_id
           or v_existing.projection_policy_key <> p_projection_policy_key
           or v_existing.projection_policy_version <> p_projection_policy_version then
            raise exception using
                errcode = '23505',
                message = 'human_handoff_command_conflict';
        end if;

        outcome := 'already_requested';
        handoff_request_id := v_existing.id;
        affected_actions := 0;
        affected_attempts := 0;
        return next;
        return;
    end if;

    select evidence.* into v_evidence
    from public.human_handoff_request_evidence evidence
    where evidence.command_key = p_command_key;

    if v_evidence.id is not null then
        select request.* into strict v_existing
        from public.human_handoff_requests request
        where request.id = v_evidence.handoff_request_id;
        if v_existing.recovery_case_id <> p_recovery_case_id
           or v_existing.projection_policy_key <> p_projection_policy_key
           or v_existing.projection_policy_version <> p_projection_policy_version
           or v_evidence.reason_code <> p_reason_code
           or v_evidence.requested_by <> p_requested_by
           or v_evidence.source_action_id is distinct from p_source_action_id
           or v_evidence.source_attempt_id is distinct from p_source_attempt_id then
            raise exception using
                errcode = '23505',
                message = 'human_handoff_command_conflict';
        end if;

        outcome := 'already_requested';
        handoff_request_id := v_existing.id;
        affected_actions := 0;
        affected_attempts := 0;
        return next;
        return;
    end if;

    select policy.* into v_policy
    from public.human_handoff_projection_policies policy
    where policy.policy_key = p_projection_policy_key
      and policy.policy_version = p_projection_policy_version
      and policy.active;

    if v_policy.id is null then
        raise exception using
            errcode = '55000',
            message = 'handoff_projection_policy_unavailable';
    end if;

    select contact.id into v_contact_id
    from public.contacts contact
    join public.recovery_cases recovery_case
      on recovery_case.contact_id = contact.id
    where recovery_case.id = p_recovery_case_id
    for update of contact;

    if v_contact_id is null then
        raise exception using
            errcode = 'P0002',
            message = 'handoff_recovery_case_not_found';
    end if;

    select recovery_case.* into v_case
    from public.recovery_cases recovery_case
    where recovery_case.id = p_recovery_case_id
      and recovery_case.contact_id = v_contact_id
    for update;

    if v_case.conversation_id is null then
        raise exception using
            errcode = '55000',
            message = 'handoff_conversation_unavailable';
    end if;

    if v_case.status in ('won', 'cancelled', 'lost', 'sequence_exhausted') then
        raise exception using
            errcode = '55000',
            message = 'handoff_case_terminal';
    end if;

    select binding.* into v_binding
    from public.pilot_recovery_case_bindings binding
    where binding.recovery_case_id = p_recovery_case_id
      and binding.scope_key = v_policy.scope_key
      and binding.scope_version = v_policy.scope_version;
    if v_binding.recovery_case_id is null then
        raise exception using
            errcode = '55000',
            message = 'handoff_pilot_scope_mismatch';
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = v_binding.scope_key
      and scope.version = v_binding.scope_version
      and scope.status = 'published'
      and scope.tenant_key = 'lancemos'
      and scope.channel = 'whatsapp'
      and scope.source = 'hotmart'
      and scope.purpose = 'cart_recovery';
    if v_scope.scope_key is null then
        raise exception using
            errcode = '55000',
            message = 'handoff_pilot_scope_unavailable';
    end if;

    perform 1
    from public.followup_sequences sequence
    where sequence.recovery_case_id = p_recovery_case_id
      and sequence.status in ('active', 'paused')
    order by sequence.id
    for update;

    select coalesce(array_agg(sequence.id order by sequence.id), '{}'::uuid[])
      into v_sequence_ids
    from public.followup_sequences sequence
    where sequence.recovery_case_id = p_recovery_case_id
      and sequence.status in ('active', 'paused');

    perform 1
    from public.scheduled_actions action
    where action.recovery_case_id = p_recovery_case_id
      and action.status in (
          'pending', 'deferred', 'retryable_failed', 'delivery_unknown'
      )
    order by action.id
    for update;

    select coalesce(array_agg(action.id order by action.id), '{}'::uuid[])
      into v_action_ids
    from public.scheduled_actions action
    where action.recovery_case_id = p_recovery_case_id
      and action.status in (
          'pending', 'deferred', 'retryable_failed', 'delivery_unknown'
      );

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

    v_now := clock_timestamp();
    if p_source_attempt_id is not null and not exists (
        select 1
        from public.followup_delivery_attempts attempt
        join public.scheduled_actions action on action.id = attempt.action_id
        where attempt.id = p_source_attempt_id
          and attempt.action_id = p_source_action_id
          and attempt.phase = 'reserved'
          and action.recovery_case_id = p_recovery_case_id
          and action.lease_owner = p_worker_id
          and action.lease_generation = p_lease_generation
          and action.lease_expires_at > v_now
    ) then
        raise exception using
            errcode = '40001',
            message = 'handoff_source_attempt_fence_rejected';
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.contact_id = v_case.contact_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active'
      and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = v_scope.chatwoot_inbox_id::text;
    if v_identity.id is null then
        raise exception using
            errcode = '55000',
            message = 'handoff_channel_identity_scope_mismatch';
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.id = v_case.conversation_id
      and conversation.contact_id = v_case.contact_id
      and conversation.channel_identity_id = v_identity.id
      and conversation.commercial_context ->> 'chatwoot_conversation_id'
          ~ '^[1-9][0-9]*$';
    if v_conversation.id is null then
        raise exception using
            errcode = '55000',
            message = 'handoff_conversation_authority_mismatch';
    end if;

    select request.* into v_existing
    from public.human_handoff_requests request
    where request.recovery_case_id = p_recovery_case_id
    order by request.created_at desc, request.id desc
    limit 1
    for update;

    if v_existing.id is null then
        insert into public.human_handoff_requests (
            recovery_case_id,
            conversation_id,
            source_action_id,
            source_attempt_id,
            command_key,
            primary_reason_code,
            requested_by,
            projection_policy_key,
            projection_policy_version,
            scope_key,
            scope_version,
            chatwoot_account_id,
            chatwoot_inbox_id,
            external_conversation_id,
            expected_team_id,
            note_template_key,
            note_template_version,
            private_note_body
        ) values (
            p_recovery_case_id,
            v_case.conversation_id,
            p_source_action_id,
            p_source_attempt_id,
            p_command_key,
            p_reason_code,
            p_requested_by,
            v_policy.policy_key,
            v_policy.policy_version,
            v_policy.scope_key,
            v_policy.scope_version,
            v_scope.chatwoot_account_id,
            v_scope.chatwoot_inbox_id,
            (v_conversation.commercial_context ->> 'chatwoot_conversation_id')::bigint,
            v_policy.expected_team_id,
            v_policy.note_template_key,
            v_policy.note_template_version,
            v_policy.private_note_body
        ) returning * into v_existing;

        insert into public.human_handoff_projection_effects (
            handoff_request_id,
            effect_kind
        ) values
            (v_existing.id, 'assignment'),
            (v_existing.id, 'private_note');
        outcome := 'requested';
    else
        if v_existing.projection_policy_key <> p_projection_policy_key
           or v_existing.projection_policy_version <> p_projection_policy_version then
            raise exception using
                errcode = '23505',
                message = 'human_handoff_command_conflict';
        end if;
        insert into public.human_handoff_request_evidence (
            handoff_request_id,
            reason_code,
            source_action_id,
            source_attempt_id,
            requested_by,
            command_key
        ) values (
            v_existing.id,
            p_reason_code,
            p_source_action_id,
            p_source_attempt_id,
            p_requested_by,
            p_command_key
        );
        outcome := 'evidence_appended';
    end if;

    update public.followup_delivery_attempts attempt
    set phase = 'completed',
        outcome = 'failed_before_request',
        reason_code = 'human_handoff_requested',
        finalized_next_attempt_at = null,
        reconciliation_deadline = null,
        updated_at = v_now
    where attempt.id = any(v_attempt_ids)
      and attempt.phase = 'reserved';
    get diagnostics affected_attempts = row_count;

    update public.followup_delivery_attempts attempt
    set phase = 'completed',
        outcome = 'delivery_unknown',
        reason_code = 'human_handoff_after_request_started',
        finalized_next_attempt_at = null,
        reconciliation_deadline = 'infinity'::timestamptz,
        updated_at = v_now
    where attempt.id = any(v_attempt_ids)
      and attempt.phase = 'request_started';
    get diagnostics v_started_attempts = row_count;
    affected_attempts := affected_attempts + v_started_attempts;

    update public.scheduled_actions action
    set status = case
            when exists (
                select 1
                from public.followup_delivery_attempts attempt
                where attempt.action_id = action.id
                  and attempt.outcome = 'delivery_unknown'
                  and attempt.reason_code = 'human_handoff_after_request_started'
            ) then 'delivery_unknown'
            else 'cancelled'
        end,
        terminal_reason = case
            when exists (
                select 1
                from public.followup_delivery_attempts attempt
                where attempt.action_id = action.id
                  and attempt.outcome = 'delivery_unknown'
                  and attempt.reason_code = 'human_handoff_after_request_started'
            ) then 'human_handoff_after_request_started'
            else 'human_handoff_requested'
        end,
        lease_owner = null,
        lease_expires_at = null,
        next_attempt_at = null,
        updated_at = v_now
    where action.id = any(v_action_ids)
      and action.status in ('pending', 'deferred', 'retryable_failed');
    get diagnostics affected_actions = row_count;

    update public.followup_sequences sequence
    set status = 'paused',
        cancel_reason = 'human_handoff_requested',
        revision = revision + 1,
        updated_at = v_now
    where sequence.id = any(v_sequence_ids)
      and sequence.status in ('active', 'paused');

    update public.recovery_cases recovery_case
    set status = 'paused',
        next_contact_at = null,
        next_contact_reason = 'human_handoff_requested',
        version = version + 1,
        updated_at = v_now
    where recovery_case.id = p_recovery_case_id
      and recovery_case.status in ('grace_period', 'active', 'paused');

    update public.conversations conversation
    set status = 'paused_human',
        automation_status = 'paused',
        version = version + 1,
        updated_at = v_now
    where conversation.id = v_case.conversation_id
      and conversation.contact_id = v_contact_id
      and conversation.channel_identity_id = v_case.selected_channel_identity_id;

    if not found then
        raise exception using
            errcode = '55000',
            message = 'handoff_conversation_authority_mismatch';
    end if;

    insert into public.conversation_events (
        conversation_id,
        recovery_case_id,
        event_type,
        actor_type,
        related_action_id,
        data
    ) values (
        v_case.conversation_id,
        p_recovery_case_id,
        'human_handoff_requested',
        case when p_requested_by = 'operator' then 'human_agent' else 'system' end,
        p_source_action_id,
        jsonb_build_object(
            'handoff_request_id', v_existing.id,
            'reason_code', p_reason_code
        )
    );

    handoff_request_id := v_existing.id;
    return next;
end;
$function$;

create or replace function public.claim_human_handoff_projection_effects(
    p_worker_id text,
    p_limit integer default 10,
    p_lease_seconds integer default 60,
    p_now timestamptz default clock_timestamp()
)
returns table (
    effect_id uuid,
    handoff_request_id uuid,
    effect_kind text,
    current_effect_status text,
    attempt_count integer,
    lease_generation bigint,
    expected_team_id bigint,
    chatwoot_account_id bigint,
    chatwoot_inbox_id bigint,
    chatwoot_conversation_id bigint,
    private_note_body text,
    idempotency_marker text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_now timestamptz := clock_timestamp();
begin
    if p_worker_id is null
       or btrim(p_worker_id) = ''
       or char_length(p_worker_id) > 200
       or p_limit is null or p_limit < 1 or p_limit > 100
       or p_lease_seconds is null
       or p_lease_seconds < 5 or p_lease_seconds > 900
       or p_now is null then
        raise exception using
            errcode = '22023',
            message = 'invalid_handoff_projection_claim_parameters';
    end if;

    return query
    with candidates as (
        select effect.id
        from public.human_handoff_projection_effects effect
        join public.human_handoff_requests request
          on request.id = effect.handoff_request_id
        join public.recovery_cases recovery_case
          on recovery_case.id = request.recovery_case_id
         and recovery_case.conversation_id = request.conversation_id
         and recovery_case.status = 'paused'
        join public.conversations conversation
          on conversation.id = request.conversation_id
         and conversation.contact_id = recovery_case.contact_id
         and conversation.channel_identity_id = recovery_case.selected_channel_identity_id
         and conversation.automation_status = 'paused'
        join public.channel_identities identity
          on identity.id = recovery_case.selected_channel_identity_id
         and identity.contact_id = recovery_case.contact_id
         and identity.channel = 'whatsapp'
         and identity.identity_status = 'active'
         and identity.account_id ~ '^chatwoot:[1-9][0-9]*$'
         and identity.metadata ->> 'inbox_id' ~ '^[1-9][0-9]*$'
        join public.pilot_recovery_case_bindings binding
          on binding.recovery_case_id = recovery_case.id
         and binding.scope_key = request.scope_key
         and binding.scope_version = request.scope_version
        join public.pilot_scope_versions scope
          on scope.scope_key = binding.scope_key
         and scope.version = binding.scope_version
         and scope.status = 'published'
         and identity.account_id = 'chatwoot:' || scope.chatwoot_account_id::text
         and identity.metadata ->> 'inbox_id' = scope.chatwoot_inbox_id::text
         and request.chatwoot_account_id = scope.chatwoot_account_id
         and request.chatwoot_inbox_id = scope.chatwoot_inbox_id
         and conversation.commercial_context ->> 'chatwoot_conversation_id' =
             request.external_conversation_id::text
        where effect.effect_status in (
            'pending', 'retryable_failed', 'delivery_unknown'
        )
          and (effect.next_attempt_at is null or effect.next_attempt_at <= v_now)
          and (effect.lease_expires_at is null or effect.lease_expires_at <= v_now)
          and request.status in ('requested', 'projection_failed', 'dead_letter')
          and conversation.commercial_context ->> 'chatwoot_conversation_id'
              ~ '^[1-9][0-9]*$'
        order by
          request.created_at,
          case effect.effect_kind when 'assignment' then 0 else 1 end,
          effect.created_at,
          effect.id
        for update of effect skip locked
        limit p_limit
    ),
    claimed as (
        update public.human_handoff_projection_effects effect
        set lease_owner = p_worker_id,
            lease_generation = effect.lease_generation + 1,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
            attempt_count = effect.attempt_count + 1,
            updated_at = v_now
        from candidates
        where effect.id = candidates.id
        returning effect.*
    )
    select
        claimed.id,
        request.id,
        claimed.effect_kind,
        claimed.effect_status,
        claimed.attempt_count,
        claimed.lease_generation,
        request.expected_team_id,
        request.chatwoot_account_id,
        request.chatwoot_inbox_id,
        request.external_conversation_id,
        request.private_note_body,
        format(
            '[supportmagician-handoff:%s:%s:v%s]',
            request.id,
            request.note_template_key,
            request.note_template_version
        )
    from claimed
    join public.human_handoff_requests request
      on request.id = claimed.handoff_request_id
    join public.recovery_cases recovery_case
      on recovery_case.id = request.recovery_case_id
     and recovery_case.conversation_id = request.conversation_id
    join public.channel_identities identity
      on identity.id = recovery_case.selected_channel_identity_id
     and identity.contact_id = recovery_case.contact_id
    join public.conversations conversation
      on conversation.id = request.conversation_id;
end;
$function$;

create or replace function public.finalize_human_handoff_projection_effect(
    p_effect_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_outcome text,
    p_error_code text default null,
    p_retry_at timestamptz default null,
    p_now timestamptz default clock_timestamp()
)
returns table (
    effect_status text,
    handoff_status text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_effect public.human_handoff_projection_effects%rowtype;
    v_request_status text;
    v_now timestamptz;
begin
    if p_effect_id is null
       or p_worker_id is null
       or btrim(p_worker_id) = ''
       or p_lease_generation is null
       or p_lease_generation < 1
       or p_outcome not in (
           'applied', 'retryable_failed', 'delivery_unknown',
           'conflict', 'dead_letter'
       )
       or p_now is null
       or (
           p_outcome in ('retryable_failed', 'delivery_unknown')
           and p_retry_at is null
       )
       or (
           p_outcome not in ('retryable_failed', 'delivery_unknown')
           and p_retry_at is not null
       )
       or (
           p_outcome <> 'applied'
           and (p_error_code is null or btrim(p_error_code) = '')
       )
       or char_length(coalesce(p_error_code, '')) > 200 then
        raise exception using
            errcode = '22023',
            message = 'invalid_handoff_projection_finalize_parameters';
    end if;

    select effect.* into v_effect
    from public.human_handoff_projection_effects effect
    where effect.id = p_effect_id
    for update;

    v_now := clock_timestamp();
    if v_effect.id is null
       or v_effect.lease_owner is distinct from p_worker_id
       or v_effect.lease_generation <> p_lease_generation
       or v_effect.lease_expires_at is null
       or v_effect.lease_expires_at <= v_now
       or v_effect.effect_status not in (
           'pending', 'retryable_failed', 'delivery_unknown'
       ) then
        raise exception using
            errcode = '40001',
            message = 'handoff_projection_lease_fence_rejected';
    end if;

    update public.human_handoff_projection_effects effect
    set effect_status = p_outcome,
        next_attempt_at = case
            when p_outcome in ('retryable_failed', 'delivery_unknown')
                then p_retry_at
            else null
        end,
        last_error_code = case
            when p_outcome = 'applied' then null
            else p_error_code
        end,
        applied_at = case
            when p_outcome = 'applied' then v_now
            else null
        end,
        lease_owner = null,
        lease_expires_at = null,
        updated_at = v_now
    where effect.id = p_effect_id;

    if not exists (
        select 1
        from public.human_handoff_projection_effects effect
        where effect.handoff_request_id = v_effect.handoff_request_id
          and effect.effect_status <> 'applied'
    ) then
        update public.human_handoff_requests request
        set status = 'projected',
            projected_at = v_now,
            last_error_code = null,
            updated_at = v_now
        where request.id = v_effect.handoff_request_id
        returning request.status into v_request_status;
    elsif exists (
        select 1
        from public.human_handoff_projection_effects effect
        where effect.handoff_request_id = v_effect.handoff_request_id
          and effect.effect_status = 'dead_letter'
    ) then
        update public.human_handoff_requests request
        set status = 'dead_letter',
            projected_at = null,
            last_error_code = p_error_code,
            updated_at = v_now
        where request.id = v_effect.handoff_request_id
        returning request.status into v_request_status;
    else
        update public.human_handoff_requests request
        set status = 'projection_failed',
            projected_at = null,
            last_error_code = p_error_code,
            updated_at = v_now
        where request.id = v_effect.handoff_request_id
        returning request.status into v_request_status;
    end if;

    effect_status := p_outcome;
    handoff_status := v_request_status;
    return next;
end;
$function$;

create or replace function public.get_human_handoff_projection_status()
returns table (
    pending_count bigint,
    retryable_count bigint,
    delivery_unknown_count bigint,
    conflict_count bigint,
    dead_letter_count bigint
)
language sql
stable
security definer
set search_path = public, pg_temp
as $function$
    select
        count(*) filter (where effect_status = 'pending'),
        count(*) filter (where effect_status = 'retryable_failed'),
        count(*) filter (where effect_status = 'delivery_unknown'),
        count(*) filter (where effect_status = 'conflict'),
        count(*) filter (where effect_status = 'dead_letter')
    from public.human_handoff_projection_effects;
$function$;

alter table public.human_handoff_projection_policies enable row level security;
alter table public.human_handoff_requests enable row level security;
alter table public.human_handoff_request_evidence enable row level security;
alter table public.human_handoff_projection_effects enable row level security;

revoke all on table public.human_handoff_projection_policies
from public;
revoke all on table public.human_handoff_requests
from public;
revoke all on table public.human_handoff_request_evidence
from public;
revoke all on table public.human_handoff_projection_effects
from public;

revoke execute on function public.request_human_handoff(
    uuid, text, text, text, text, integer,
    uuid, uuid, text, bigint, timestamptz
) from public;
revoke execute on function public.claim_human_handoff_projection_effects(
    text, integer, integer, timestamptz
) from public;
revoke execute on function public.finalize_human_handoff_projection_effect(
    uuid, text, bigint, text, text, timestamptz, timestamptz
) from public;
revoke execute on function public.get_human_handoff_projection_status()
from public;
revoke execute on function public.protect_human_handoff_policy_version()
from public;
revoke execute on function public.protect_human_handoff_request_identity()
from public;
revoke execute on function public.reject_human_handoff_evidence_mutation()
from public;
revoke execute on function public.protect_handoff_projection_effect_identity()
from public;

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
            'revoke all on table public.human_handoff_projection_policies from %I',
            v_role
        );
        execute format(
            'revoke all on table public.human_handoff_requests from %I',
            v_role
        );
        execute format(
            'revoke all on table public.human_handoff_request_evidence from %I',
            v_role
        );
        execute format(
            'revoke all on table public.human_handoff_projection_effects from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.request_human_handoff(uuid, text, text, text, text, integer, uuid, uuid, text, bigint, timestamptz) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.finalize_human_handoff_projection_effect(uuid, text, bigint, text, text, timestamptz, timestamptz) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.get_human_handoff_projection_status() from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.protect_human_handoff_policy_version() from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.protect_human_handoff_request_identity() from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.reject_human_handoff_evidence_mutation() from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.protect_handoff_projection_effect_identity() from %I',
            v_role
        );
    end loop;

    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.request_human_handoff(
            uuid, text, text, text, text, integer,
            uuid, uuid, text, bigint, timestamptz
        ) to service_role;
        grant execute on function public.claim_human_handoff_projection_effects(
            text, integer, integer, timestamptz
        ) to service_role;
        grant execute on function public.finalize_human_handoff_projection_effect(
            uuid, text, bigint, text, text, timestamptz, timestamptz
        ) to service_role;
        grant execute on function public.get_human_handoff_projection_status()
        to service_role;
    end if;
end;
$roles$;

commit;
