-- Anchor durable handoff to the commercial-case root and admit inbound handoff.
-- Additive only: no policy or Chatwoot team is seeded by this migration.

begin;

alter table public.human_handoff_projection_policies
    alter column scope_key drop not null,
    alter column scope_version drop not null,
    add column inbound_scope_key text,
    add column inbound_scope_version integer,
    add constraint human_handoff_projection_policy_inbound_scope_fk
        foreign key (inbound_scope_key, inbound_scope_version)
        references public.inbound_commercial_scope_versions(scope_key, version)
        on delete restrict,
    add constraint human_handoff_projection_policy_scope_shape check (
        num_nonnulls(scope_key, inbound_scope_key) = 1
        and num_nonnulls(scope_version, inbound_scope_version) = 1
        and ((scope_key is null) = (scope_version is null))
        and ((inbound_scope_key is null) = (inbound_scope_version is null))
        and (inbound_scope_key is null
             or inbound_scope_key ~ '^[a-z0-9_-]{1,100}$')
        and (inbound_scope_version is null or inbound_scope_version > 0)
    );

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
       and new.scope_key is not distinct from old.scope_key
       and new.scope_version is not distinct from old.scope_version
       and new.inbound_scope_key is not distinct from old.inbound_scope_key
       and new.inbound_scope_version is not distinct from old.inbound_scope_version
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

alter table public.human_handoff_requests
    add column commercial_case_id uuid;

update public.human_handoff_requests
set commercial_case_id = recovery_case_id;

alter table public.human_handoff_requests
    alter column commercial_case_id set not null,
    alter column recovery_case_id drop not null,
    alter column scope_key drop not null,
    alter column scope_version drop not null,
    add column inbound_scope_key text,
    add column inbound_scope_version integer,
    add constraint human_handoff_requests_commercial_case_fk
        foreign key (commercial_case_id)
        references public.commercial_cases(id) on delete restrict,
    add constraint human_handoff_requests_inbound_scope_fk
        foreign key (inbound_scope_key, inbound_scope_version)
        references public.inbound_commercial_scope_versions(scope_key, version)
        on delete restrict,
    add constraint human_handoff_request_aggregate_shape check (
        recovery_case_id is null or commercial_case_id = recovery_case_id
    ),
    add constraint human_handoff_request_scope_shape check (
        num_nonnulls(scope_key, inbound_scope_key) = 1
        and num_nonnulls(scope_version, inbound_scope_version) = 1
        and ((scope_key is null) = (scope_version is null))
        and ((inbound_scope_key is null) = (inbound_scope_version is null))
    );

drop index public.human_handoff_requests_one_live_per_case_idx;

create unique index human_handoff_requests_one_live_per_commercial_case_idx
on public.human_handoff_requests (commercial_case_id)
where status in ('requested', 'projection_failed');

create or replace function public.bind_and_validate_handoff_commercial_case()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.commercial_cases%rowtype;
begin
    if tg_op = 'UPDATE'
       and new.commercial_case_id is distinct from old.commercial_case_id then
        raise exception using errcode = '55000',
            message = 'human_handoff_request_identity_is_immutable';
    end if;

    if new.commercial_case_id is null and new.recovery_case_id is not null then
        new.commercial_case_id := new.recovery_case_id;
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.id = new.commercial_case_id;

    if v_case.id is null
       or not (
           (v_case.case_kind = 'cart_recovery'
            and new.recovery_case_id = v_case.id
            and new.scope_key is not null
            and new.inbound_scope_key is null)
           or
           (v_case.case_kind = 'inbound_sales'
            and new.recovery_case_id is null
            and new.scope_key is null
            and new.inbound_scope_key is not null)
       ) then
        raise exception using errcode = '23514',
            message = 'human_handoff_request_aggregate_mismatch';
    end if;

    return new;
end;
$function$;

create trigger human_handoff_requests_bind_commercial_case
before insert or update on public.human_handoff_requests
for each row execute function public.bind_and_validate_handoff_commercial_case();

create or replace function public.protect_human_handoff_request_identity()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE'
       or new.id is distinct from old.id
       or new.commercial_case_id is distinct from old.commercial_case_id
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
       or new.inbound_scope_key is distinct from old.inbound_scope_key
       or new.inbound_scope_version is distinct from old.inbound_scope_version
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

create or replace function public.protect_inbound_commercial_case()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'INSERT' then
        if new.case_kind <> 'inbound_sales'
           or new.recovery_case_id is not null
           or new.status <> 'active'
           or new.automation_status <> 'draft_only'
           or new.identity_resolution_status <> 'resolved'
           or new.authority_mode <> 'shadow'
           or new.version <> 1
           or new.selected_channel_identity_id is null
           or new.conversation_id is null
           or new.inbound_scope_key is null
           or new.inbound_scope_version is null
           or nullif(btrim(new.tenant_ref), '') is null
           or nullif(btrim(new.product_ref), '') is null
           or nullif(btrim(new.offer_ref), '') is null then
            raise exception using errcode = '23514',
                message = 'invalid_inbound_commercial_case_state';
        end if;
        if not exists (
            select 1
            from public.inbound_commercial_scope_versions scope
            join public.channel_identities identity
              on identity.channel = 'whatsapp'
             and identity.account_id = 'chatwoot:' || scope.chatwoot_account_id::text
             and identity.metadata ->> 'inbox_id' = scope.chatwoot_inbox_id::text
            join public.conversations conversation
              on conversation.channel_identity_id = identity.id
             and conversation.contact_id = identity.contact_id
            where scope.scope_key = new.inbound_scope_key
              and scope.version = new.inbound_scope_version
              and scope.status = 'published'
              and scope.tenant_key = new.tenant_ref
              and scope.external_product_id = new.product_ref
              and scope.offer_code = new.offer_ref
              and identity.id = new.selected_channel_identity_id
              and identity.contact_id = new.contact_id
              and identity.identity_status = 'active'
              and conversation.id = new.conversation_id
              and conversation.commercial_context = jsonb_build_object(
                  'chatwoot_conversation_id',
                  conversation.commercial_context ->> 'chatwoot_conversation_id'
              )
        ) then
            raise exception using errcode = '23514',
                message = 'inbound_commercial_case_canonical_mismatch';
        end if;
        return new;
    end if;

    if tg_op = 'UPDATE'
       and old.case_kind = 'inbound_sales'
       and old.status = 'active'
       and old.automation_status = 'draft_only'
       and new.status = 'paused'
       and new.automation_status = 'disabled'
       and new.version = old.version + 1
       and new.updated_at > old.updated_at
       and new.id = old.id
       and new.recovery_case_id is not distinct from old.recovery_case_id
       and new.case_kind = old.case_kind
       and new.contact_id = old.contact_id
       and new.selected_channel_identity_id = old.selected_channel_identity_id
       and new.conversation_id = old.conversation_id
       and new.product_ref is not distinct from old.product_ref
       and new.offer_ref is not distinct from old.offer_ref
       and new.identity_resolution_status is not distinct from old.identity_resolution_status
       and new.authority_mode = old.authority_mode
       and new.created_at = old.created_at
       and new.inbound_scope_key = old.inbound_scope_key
       and new.inbound_scope_version = old.inbound_scope_version
       and new.tenant_ref = old.tenant_ref then
        return new;
    end if;

    raise exception using errcode = '55000',
        message = 'inbound_commercial_case_is_immutable';
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
set search_path = pg_catalog, public, pg_temp
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
        raise exception using errcode = '22023',
            message = 'invalid_handoff_projection_claim_parameters';
    end if;

    return query
    with candidates as (
        select effect.id
        from public.human_handoff_projection_effects effect
        join public.human_handoff_requests request
          on request.id = effect.handoff_request_id
        join public.commercial_cases commercial_case
          on commercial_case.id = request.commercial_case_id
        left join public.recovery_cases recovery_case
          on recovery_case.id = request.recovery_case_id
         and recovery_case.commercial_case_id = commercial_case.id
        join public.conversations conversation
          on conversation.id = request.conversation_id
         and conversation.id = commercial_case.conversation_id
         and conversation.contact_id = commercial_case.contact_id
         and conversation.channel_identity_id = commercial_case.selected_channel_identity_id
         and conversation.status = 'paused_human'
         and conversation.automation_status = 'paused'
        join public.channel_identities identity
          on identity.id = commercial_case.selected_channel_identity_id
         and identity.contact_id = commercial_case.contact_id
         and identity.channel = 'whatsapp'
         and identity.identity_status = 'active'
        left join public.pilot_recovery_case_bindings pilot_binding
          on pilot_binding.recovery_case_id = request.recovery_case_id
         and pilot_binding.scope_key = request.scope_key
         and pilot_binding.scope_version = request.scope_version
        left join public.pilot_scope_versions pilot_scope
          on pilot_scope.scope_key = pilot_binding.scope_key
         and pilot_scope.version = pilot_binding.scope_version
         and pilot_scope.status = 'published'
        left join public.inbound_commercial_case_admissions inbound_admission
          on inbound_admission.commercial_case_id = commercial_case.id
         and inbound_admission.conversation_id = request.conversation_id
         and inbound_admission.channel_identity_id = identity.id
         and inbound_admission.scope_key = request.inbound_scope_key
         and inbound_admission.scope_version = request.inbound_scope_version
        left join public.inbound_commercial_scope_versions inbound_scope
          on inbound_scope.scope_key = inbound_admission.scope_key
         and inbound_scope.version = inbound_admission.scope_version
         and inbound_scope.status = 'published'
        where effect.effect_status in (
            'pending', 'retryable_failed', 'delivery_unknown'
        )
          and (effect.next_attempt_at is null or effect.next_attempt_at <= v_now)
          and (effect.lease_expires_at is null or effect.lease_expires_at <= v_now)
          and request.status in ('requested', 'projection_failed', 'dead_letter')
          and request.chatwoot_account_id > 0
          and request.chatwoot_inbox_id > 0
          and request.external_conversation_id > 0
          and conversation.commercial_context ->> 'chatwoot_conversation_id'
              = request.external_conversation_id::text
          and (
              (
                  request.recovery_case_id is not null
                  and commercial_case.case_kind = 'cart_recovery'
                  and commercial_case.id = request.recovery_case_id
                  and commercial_case.status = 'paused'
                  and commercial_case.automation_status = 'paused'
                  and recovery_case.status = 'paused'
                  and recovery_case.conversation_id = request.conversation_id
                  and pilot_scope.scope_key is not null
                  and request.inbound_scope_key is null
                  and identity.account_id =
                      'chatwoot:' || pilot_scope.chatwoot_account_id::text
                  and identity.metadata ->> 'inbox_id' =
                      pilot_scope.chatwoot_inbox_id::text
                  and request.chatwoot_account_id = pilot_scope.chatwoot_account_id
                  and request.chatwoot_inbox_id = pilot_scope.chatwoot_inbox_id
              )
              or
              (
                  request.recovery_case_id is null
                  and commercial_case.case_kind = 'inbound_sales'
                  and commercial_case.status = 'paused'
                  and commercial_case.automation_status = 'disabled'
                  and inbound_scope.scope_key is not null
                  and request.scope_key is null
                  and conversation.commercial_context = jsonb_build_object(
                      'chatwoot_conversation_id',
                      request.external_conversation_id::text
                  )
                  and identity.account_id =
                      'chatwoot:' || inbound_scope.chatwoot_account_id::text
                  and identity.metadata ->> 'inbox_id' =
                      inbound_scope.chatwoot_inbox_id::text
                  and request.chatwoot_account_id = inbound_scope.chatwoot_account_id
                  and request.chatwoot_inbox_id = inbound_scope.chatwoot_inbox_id
              )
          )
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
      on request.id = claimed.handoff_request_id;
end;
$function$;

create or replace function public.request_inbound_human_handoff(
    p_commercial_case_id uuid,
    p_command_key text,
    p_reason_code text,
    p_projection_policy_key text,
    p_projection_policy_version integer,
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
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_contact_id uuid;
    v_case public.commercial_cases%rowtype;
    v_policy public.human_handoff_projection_policies%rowtype;
    v_admission public.inbound_commercial_case_admissions%rowtype;
    v_scope public.inbound_commercial_scope_versions%rowtype;
    v_identity public.channel_identities%rowtype;
    v_conversation public.conversations%rowtype;
    v_request public.human_handoff_requests%rowtype;
    v_now timestamptz;
begin
    if p_commercial_case_id is null
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_reason_code not in (
           'explicit_human_request',
           'commercial_exception',
           'policy_requires_human'
       )
       or p_projection_policy_key is null
       or p_projection_policy_key !~ '^[a-z0-9_-]{1,100}$'
       or p_projection_policy_version is null
       or p_projection_policy_version < 1
       or p_now is null then
        raise exception using errcode = '22023',
            message = 'invalid_inbound_human_handoff_parameters';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('human_handoff_command:' || p_command_key, 0)
    );

    select request.* into v_request
    from public.human_handoff_requests request
    where request.command_key = p_command_key;

    if v_request.id is not null then
        if v_request.commercial_case_id <> p_commercial_case_id
           or v_request.recovery_case_id is not null
           or v_request.primary_reason_code <> p_reason_code
           or v_request.requested_by <> 'agent'
           or v_request.source_action_id is not null
           or v_request.source_attempt_id is not null
           or v_request.projection_policy_key <> p_projection_policy_key
           or v_request.projection_policy_version <> p_projection_policy_version then
            raise exception using errcode = '23505',
                message = 'human_handoff_command_conflict';
        end if;
        outcome := 'already_requested';
        handoff_request_id := v_request.id;
        affected_actions := 0;
        affected_attempts := 0;
        return next;
        return;
    end if;

    select commercial_case.contact_id into v_contact_id
    from public.commercial_cases commercial_case
    where commercial_case.id = p_commercial_case_id
      and commercial_case.case_kind = 'inbound_sales';
    if v_contact_id is null then
        raise exception using errcode = 'P0002',
            message = 'inbound_handoff_commercial_case_not_found';
    end if;

    perform 1 from public.contacts contact
    where contact.id = v_contact_id
    for update;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.id = p_commercial_case_id
      and commercial_case.contact_id = v_contact_id
      and commercial_case.case_kind = 'inbound_sales'
    for update;

    if v_case.id is null then
        raise exception using errcode = 'P0002',
            message = 'inbound_handoff_commercial_case_not_found';
    end if;
    if v_case.status <> 'active'
       or v_case.automation_status <> 'draft_only' then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_case_not_active_draft_only';
    end if;

    select policy.* into v_policy
    from public.human_handoff_projection_policies policy
    where policy.policy_key = p_projection_policy_key
      and policy.policy_version = p_projection_policy_version
      and policy.active
      and policy.scope_key is null
      and policy.scope_version is null
      and policy.inbound_scope_key = v_case.inbound_scope_key
      and policy.inbound_scope_version = v_case.inbound_scope_version;
    if v_policy.id is null then
        raise exception using errcode = '55000',
            message = 'handoff_projection_policy_unavailable';
    end if;

    select admission.* into v_admission
    from public.inbound_commercial_case_admissions admission
    where admission.commercial_case_id = v_case.id
      and admission.contact_id = v_case.contact_id
      and admission.channel_identity_id = v_case.selected_channel_identity_id
      and admission.conversation_id = v_case.conversation_id
      and admission.scope_key = v_case.inbound_scope_key
      and admission.scope_version = v_case.inbound_scope_version
    for share;
    if v_admission.id is null then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_admission_mismatch';
    end if;

    select scope.* into v_scope
    from public.inbound_commercial_scope_versions scope
    where scope.scope_key = v_admission.scope_key
      and scope.version = v_admission.scope_version
      and scope.status = 'published'
      and scope.tenant_key = v_case.tenant_ref
      and scope.external_product_id = v_case.product_ref
      and scope.offer_code = v_case.offer_ref
    for share;
    if v_scope.scope_key is null then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_scope_unavailable';
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.contact_id = v_case.contact_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active'
      and identity.external_user_id = v_admission.external_user_id
      and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = v_scope.chatwoot_inbox_id::text
    for share;
    if v_identity.id is null then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_identity_mismatch';
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.id = v_case.conversation_id
      and conversation.contact_id = v_case.contact_id
      and conversation.channel_identity_id = v_identity.id
      and conversation.commercial_context = jsonb_build_object(
          'chatwoot_conversation_id', v_admission.external_conversation_id::text
      )
      and conversation.status in (
          'active', 'awaiting_agent', 'awaiting_contact', 'snoozed'
      )
      and conversation.automation_status = 'draft_only'
    for update;
    if v_conversation.id is null then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_conversation_mismatch';
    end if;

    select request.* into v_request
    from public.human_handoff_requests request
    where request.commercial_case_id = v_case.id
      and request.status in ('requested', 'projection_failed')
    for update;
    if v_request.id is not null then
        raise exception using errcode = '23505',
            message = 'inbound_handoff_live_request_conflict';
    end if;

    v_now := clock_timestamp();
    update public.commercial_cases commercial_case
    set status = 'paused',
        automation_status = 'disabled',
        version = commercial_case.version + 1,
        updated_at = v_now
    where commercial_case.id = v_case.id
      and commercial_case.status = 'active'
      and commercial_case.automation_status = 'draft_only';
    if not found then
        raise exception using errcode = '40001',
            message = 'inbound_handoff_case_transition_rejected';
    end if;

    update public.conversations conversation
    set status = 'paused_human',
        automation_status = 'paused',
        human_takeover = true,
        version = conversation.version + 1,
        updated_at = v_now
    where conversation.id = v_conversation.id
      and conversation.status in (
          'active', 'awaiting_agent', 'awaiting_contact', 'snoozed'
      )
      and conversation.automation_status = 'draft_only';
    if not found then
        raise exception using errcode = '40001',
            message = 'inbound_handoff_conversation_transition_rejected';
    end if;

    insert into public.human_handoff_requests (
        commercial_case_id, recovery_case_id, conversation_id,
        source_action_id, source_attempt_id, command_key,
        primary_reason_code, requested_by,
        projection_policy_key, projection_policy_version,
        scope_key, scope_version, inbound_scope_key, inbound_scope_version,
        chatwoot_account_id, chatwoot_inbox_id, external_conversation_id,
        expected_team_id, note_template_key, note_template_version,
        private_note_body
    ) values (
        v_case.id, null, v_conversation.id,
        null, null, p_command_key,
        p_reason_code, 'agent',
        v_policy.policy_key, v_policy.policy_version,
        null, null, v_policy.inbound_scope_key, v_policy.inbound_scope_version,
        v_scope.chatwoot_account_id, v_scope.chatwoot_inbox_id,
        v_admission.external_conversation_id,
        v_policy.expected_team_id, v_policy.note_template_key,
        v_policy.note_template_version, v_policy.private_note_body
    ) returning * into v_request;

    insert into public.human_handoff_projection_effects (
        handoff_request_id, effect_kind
    ) values
        (v_request.id, 'assignment'),
        (v_request.id, 'private_note');

    outcome := 'requested';
    handoff_request_id := v_request.id;
    affected_actions := 0;
    affected_attempts := 0;
    return next;
end;
$function$;

-- The historical cart-recovery admission remains present with its exact signature.
-- Its inserts are bound to commercial_case_id by the BEFORE trigger above.

revoke execute on function public.request_inbound_human_handoff(
    uuid, text, text, text, integer, timestamptz
) from public;
revoke execute on function public.bind_and_validate_handoff_commercial_case()
from public;

revoke all on table public.human_handoff_projection_policies from public;
revoke all on table public.human_handoff_requests from public;

-- Supabase API roles are optional in role-neutral PostgreSQL test stacks.
do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.request_inbound_human_handoff(
            uuid, text, text, text, integer, timestamptz
        ) from anon;
        revoke execute on function public.bind_and_validate_handoff_commercial_case()
        from anon;
        revoke all on table public.human_handoff_projection_policies from anon;
        revoke all on table public.human_handoff_requests from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.request_inbound_human_handoff(
            uuid, text, text, text, integer, timestamptz
        ) from authenticated;
        revoke execute on function public.bind_and_validate_handoff_commercial_case()
        from authenticated;
        revoke all on table public.human_handoff_projection_policies from authenticated;
        revoke all on table public.human_handoff_requests from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke execute on function public.bind_and_validate_handoff_commercial_case()
        from service_role;
        grant execute on function public.request_inbound_human_handoff(
            uuid, text, text, text, integer, timestamptz
        ) to service_role;
    end if;
end;
$roles$;

commit;
