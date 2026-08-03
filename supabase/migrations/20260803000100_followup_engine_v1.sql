-- Motor durable de próxima acción V1.
-- Delta sobre supabase/baseline/20260803_public_schema.sql.
-- No aplicar sin preflight de filas existentes y backup administrado.

begin;

do $preflight$
begin
    if exists (select 1 from public.recovery_cases limit 1)
       or exists (select 1 from public.followup_sequences limit 1)
       or exists (select 1 from public.scheduled_actions limit 1) then
        raise exception using
            errcode = '55000',
            message = 'followup_engine_requires_empty_legacy_scheduler_tables';
    end if;
end;
$preflight$;

create table public.followup_policy_versions (
    policy_key text not null,
    version integer not null check (version > 0),
    status text not null check (status = any (array['draft', 'published', 'retired'])),
    purpose text not null check (purpose = 'cart_recovery'),
    timezone text not null,
    business_windows jsonb not null,
    grace_period interval not null check (grace_period >= interval '0 seconds'),
    expires_after interval not null check (expires_after > interval '0 seconds'),
    max_automatic_messages integer not null check (max_automatic_messages > 0),
    steps jsonb not null,
    approved_by text,
    approved_at timestamptz,
    published_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (policy_key, version),
    check (
        status <> 'published'
        or (
            approved_by is not null
            and approved_at is not null
            and published_at is not null
        )
    )
);

alter table public.followup_policy_versions enable row level security;

create or replace function public.protect_published_followup_policy()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $function$
begin
    if old.status = 'published' then
        raise exception using
            errcode = '55000',
            message = 'published_followup_policy_is_immutable';
    end if;

    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end;
$function$;

create trigger followup_policy_versions_immutable
before update or delete on public.followup_policy_versions
for each row execute function public.protect_published_followup_policy();

create trigger followup_policy_versions_set_updated_at
before update on public.followup_policy_versions
for each row execute function public.set_updated_at();

create table public.contact_authorizations (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null
        references public.contacts(id) on delete cascade,
    channel text not null
        check (channel = any (array['whatsapp', 'email', 'sms', 'instagram'])),
    purpose text not null
        check (purpose = 'cart_recovery'),
    authorization_status text not null
        check (authorization_status = any (array['allowed', 'denied', 'restricted', 'unknown'])),
    authorization_source text not null
        check (authorization_source = any (array[
            'hotmart', 'manual', 'crm', 'legal', 'system'
        ])),
    evidence jsonb not null default '{}'::jsonb,
    valid_from timestamptz not null,
    valid_until timestamptz,
    recorded_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    check (valid_until is null or valid_until > valid_from)
);

create index contact_authorizations_lookup_idx
on public.contact_authorizations (
    contact_id,
    channel,
    purpose,
    valid_from desc,
    recorded_at desc
);

alter table public.contact_authorizations enable row level security;

alter table public.recovery_cases
    add column policy_key text,
    add column policy_version integer;

alter table public.recovery_cases
    alter column policy_key set not null,
    alter column policy_version set not null;

alter table public.recovery_cases
    add constraint recovery_cases_policy_version_fkey
    foreign key (policy_key, policy_version)
    references public.followup_policy_versions(policy_key, version)
    on delete restrict;

alter table public.recovery_cases
    add constraint recovery_cases_engine_identity_unique
    unique (id, policy_key, policy_version);

alter table public.recovery_cases
    drop constraint recovery_cases_status_check;

alter table public.recovery_cases
    add constraint recovery_cases_status_check
    check (status = any (array[
        'grace_period', 'active', 'paused', 'won', 'sequence_exhausted',
        'lost', 'cancelled', 'unreachable', 'error', 'expired', 'escalated'
    ]));

alter table public.followup_sequences
    add column automatic_messages_accepted integer not null default 0
        check (automatic_messages_accepted >= 0),
    add column completion_reason text,
    add column revision bigint not null default 1 check (revision > 0);

alter table public.followup_sequences
    add constraint followup_sequences_policy_version_fkey
    foreign key (policy_key, policy_version)
    references public.followup_policy_versions(policy_key, version)
    on delete restrict;

alter table public.followup_sequences
    add constraint followup_sequences_engine_identity_unique
    unique (id, recovery_case_id, policy_key, policy_version);

alter table public.followup_sequences
    add constraint followup_sequences_case_policy_fkey
    foreign key (recovery_case_id, policy_key, policy_version)
    references public.recovery_cases (id, policy_key, policy_version)
    on delete cascade;

alter table public.scheduled_actions
    drop constraint scheduled_actions_status_check,
    drop constraint scheduled_actions_action_type_check;

alter table public.scheduled_actions
    add column policy_key text,
    add column policy_version integer,
    add column step_key text,
    add column anchor_type text,
    add column anchor_subject_internal_id uuid,
    add column anchor_observed_at timestamptz,
    add column anchor_checkpoint jsonb not null default '{}'::jsonb,
    add column next_attempt_at timestamptz,
    add column lease_owner text,
    add column lease_generation bigint not null default 0
        check (lease_generation >= 0),
    add column lease_expires_at timestamptz,
    add column terminal_reason text;

alter table public.scheduled_actions
    add constraint scheduled_actions_policy_version_fkey
    foreign key (policy_key, policy_version)
    references public.followup_policy_versions(policy_key, version)
    on delete restrict,
    add constraint scheduled_actions_action_type_check
    check (action_type = any (array[
        'first_contact_review', 'no_reply_review', 'reconcile_delivery'
    ])),
    add constraint scheduled_actions_status_check
    check (status = any (array[
        'pending', 'deferred', 'retryable_failed', 'delivery_unknown',
        'accepted_by_chatwoot', 'cancelled', 'skipped', 'expired',
        'permanent_failed', 'superseded'
    ])),
    add constraint scheduled_actions_lease_check
    check (
        (lease_owner is null and lease_expires_at is null)
        or (lease_owner is not null and lease_expires_at is not null)
    );

alter table public.scheduled_actions
    alter column followup_sequence_id set not null,
    alter column policy_key set not null,
    alter column policy_version set not null,
    alter column step_key set not null,
    alter column anchor_type set not null,
    alter column anchor_subject_internal_id set not null,
    alter column anchor_observed_at set not null,
    alter column expires_at set not null;

alter table public.scheduled_actions
    add constraint scheduled_actions_due_before_expiry_check
    check (expires_at is null or due_at <= expires_at);

alter table public.scheduled_actions
    add constraint scheduled_actions_sequence_identity_fkey
    foreign key (followup_sequence_id, recovery_case_id, policy_key, policy_version)
    references public.followup_sequences (
        id, recovery_case_id, policy_key, policy_version
    )
    on delete cascade;

drop index public.scheduled_actions_cron_job_idx;
alter table public.scheduled_actions drop column cron_job_id;
alter table public.scheduled_actions drop column not_before;

drop index public.scheduled_actions_due_idx;
drop index public.scheduled_actions_one_live_per_case_idx;

create index scheduled_actions_dispatch_idx
on public.scheduled_actions (
    coalesce(next_attempt_at, due_at),
    due_at
)
where status = any (array['pending', 'deferred', 'retryable_failed']);

create unique index scheduled_actions_one_live_per_sequence_idx
on public.scheduled_actions (followup_sequence_id)
where status = any (array[
    'pending', 'deferred', 'retryable_failed', 'delivery_unknown'
]);

create or replace function public.protect_scheduled_action_identity()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if new.recovery_case_id is distinct from old.recovery_case_id
       or new.followup_sequence_id is distinct from old.followup_sequence_id
       or new.policy_key is distinct from old.policy_key
       or new.policy_version is distinct from old.policy_version
       or new.step_key is distinct from old.step_key
       or new.action_type is distinct from old.action_type
       or new.idempotency_key is distinct from old.idempotency_key then
        raise exception using
            errcode = '55000',
            message = 'scheduled_action_identity_is_immutable';
    end if;
    return new;
end;
$function$;

create trigger scheduled_actions_protect_identity
before update on public.scheduled_actions
for each row execute function public.protect_scheduled_action_identity();

create table public.recovery_case_events (
    recovery_case_id uuid not null
        references public.recovery_cases(id) on delete cascade,
    webhook_event_id uuid not null
        references public.webhook_events(id) on delete restrict,
    event_role text not null check (event_role = 'cart_abandonment'),
    observed_at timestamptz not null,
    created_at timestamptz not null default now(),
    primary key (recovery_case_id, webhook_event_id),
    unique (webhook_event_id)
);

alter table public.recovery_case_events enable row level security;

create table public.followup_delivery_attempts (
    id uuid primary key default gen_random_uuid(),
    action_id uuid not null
        references public.scheduled_actions(id) on delete restrict,
    idempotency_key text not null,
    attempt_number integer not null check (attempt_number > 0),
    channel text not null check (channel = 'whatsapp'),
    mode text not null check (mode = any (array['freeform', 'approved_template'])),
    phase text not null default 'reserved'
        check (phase = any (array['reserved', 'request_started', 'completed'])),
    outcome text check (outcome = any (array[
        'accepted_by_chatwoot', 'rejected', 'failed_before_request',
        'delivery_unknown'
    ])),
    remote_message_id text,
    accepted_message_id uuid,
    started_at timestamptz not null,
    request_started_at timestamptz,
    accepted_at timestamptz,
    reason_code text,
    finalized_next_attempt_at timestamptz,
    reconciliation_deadline timestamptz,
    reconciliation_resolution text check (reconciliation_resolution = any (array[
        'accepted_by_chatwoot', 'not_applied', 'escalated'
    ])),
    reconciliation_next_attempt_at timestamptz,
    reconciled_at timestamptz,
    lease_generation bigint not null check (lease_generation > 0),
    expected_case_version bigint not null check (expected_case_version > 0),
    expected_sequence_revision bigint not null check (expected_sequence_revision > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (action_id, attempt_number),
    unique (action_id, lease_generation),
    check (
        (phase = 'reserved' and request_started_at is null and outcome is null)
        or (phase = 'request_started' and request_started_at is not null and outcome is null)
        or (phase = 'completed' and outcome is not null)
    ),
    check (
        outcome <> 'accepted_by_chatwoot'
        or (
            remote_message_id is not null
            and accepted_message_id is not null
            and accepted_at is not null
        )
    ),
    check (
        outcome is null
        or (outcome = 'failed_before_request' and request_started_at is null)
        or (
            outcome in ('accepted_by_chatwoot', 'rejected', 'delivery_unknown')
            and request_started_at is not null
        )
    ),
    check (
        outcome is distinct from 'delivery_unknown'
        or reconciliation_deadline is not null
    ),
    check (
        (reconciliation_resolution is null and reconciled_at is null)
        or (reconciliation_resolution is not null and reconciled_at is not null)
    ),
    check (
        reconciliation_resolution is null
        or (reconciliation_resolution = 'accepted_by_chatwoot' and outcome = 'accepted_by_chatwoot')
        or (reconciliation_resolution = 'not_applied' and outcome = 'rejected')
        or (reconciliation_resolution = 'escalated' and outcome = 'delivery_unknown')
    ),
    check (
        (reconciliation_resolution = 'not_applied' and reconciliation_next_attempt_at is not null)
        or (reconciliation_resolution is distinct from 'not_applied' and reconciliation_next_attempt_at is null)
    )
);

alter table public.followup_delivery_attempts enable row level security;

create index followup_delivery_attempts_in_flight_idx
on public.followup_delivery_attempts (action_id)
where phase = 'request_started';

create trigger followup_delivery_attempts_set_updated_at
before update on public.followup_delivery_attempts
for each row execute function public.set_updated_at();

create or replace function public.plan_cart_recovery(
    p_webhook_event_id uuid,
    p_contact_id uuid,
    p_external_product_id text,
    p_product_name text,
    p_offer_code text,
    p_policy_key text,
    p_policy_version integer,
    p_abandoned_at timestamptz
)
returns table (
    recovery_case_id uuid,
    followup_sequence_id uuid,
    scheduled_action_id uuid,
    created boolean
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_policy public.followup_policy_versions%rowtype;
    v_event public.webhook_events%rowtype;
    v_case_id uuid;
    v_sequence_id uuid;
    v_action_id uuid;
    v_existing_event_case_id uuid;
    v_case_status text;
    v_case_version bigint;
    v_case_policy_key text;
    v_case_policy_version integer;
    v_sequence_status text;
    v_original_abandoned_at timestamptz;
begin
    if p_external_product_id is null or btrim(p_external_product_id) = '' then
        raise exception using errcode = '22023', message = 'external_product_id_required';
    end if;
    if p_product_name is null or btrim(p_product_name) = '' then
        raise exception using errcode = '22023', message = 'product_name_required';
    end if;

    select * into strict v_policy
    from public.followup_policy_versions
    where policy_key = p_policy_key
      and version = p_policy_version
      and status = 'published'
      and purpose = 'cart_recovery';

    select * into strict v_event
    from public.webhook_events
    where id = p_webhook_event_id
      and source in ('hotmart', 'simulator')
      and event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
    for update;

    perform 1
    from public.contacts
    where id = p_contact_id
    for update;
    if not found then
        raise exception using errcode = '23503', message = 'contact_not_found';
    end if;

    select rce.recovery_case_id
      into v_existing_event_case_id
    from public.recovery_case_events rce
    where rce.webhook_event_id = p_webhook_event_id;

    if v_existing_event_case_id is not null then
        select fs.id, sa.id
          into strict v_sequence_id, v_action_id
        from public.followup_sequences fs
        join public.scheduled_actions sa
          on sa.followup_sequence_id = fs.id
        where fs.recovery_case_id = v_existing_event_case_id
        order by sa.created_at asc
        limit 1;

        return query
        select v_existing_event_case_id, v_sequence_id, v_action_id, false;
        return;
    end if;

    select rc.id, rc.status, rc.policy_key, rc.policy_version
      into v_case_id, v_case_status, v_case_policy_key, v_case_policy_version
    from public.recovery_cases rc
    where rc.contact_id = p_contact_id
      and rc.source = v_event.source
      and rc.external_product_id = p_external_product_id
      and rc.offer_code is not distinct from p_offer_code
      and rc.status in ('grace_period', 'active', 'paused')
    order by rc.created_at asc
    limit 1
    for update;

    if v_case_id is not null then
        insert into public.recovery_case_events (
            recovery_case_id,
            webhook_event_id,
            event_role,
            observed_at
        ) values (
            v_case_id,
            p_webhook_event_id,
            'cart_abandonment',
            p_abandoned_at
        );

        update public.recovery_cases
        set context = context || jsonb_build_object(
                'latest_abandonment_event_id', p_webhook_event_id,
                'latest_abandonment_at', p_abandoned_at
            ),
            version = version + 1
        where id = v_case_id
        returning version into v_case_version;

        select fs.id, fs.status into v_sequence_id, v_sequence_status
        from public.followup_sequences fs
        where fs.recovery_case_id = v_case_id
        order by fs.created_at desc
        limit 1
        for update;

        if v_sequence_id is not null then
            select sa.id into v_action_id
            from public.scheduled_actions sa
            where sa.followup_sequence_id = v_sequence_id
              and sa.status in (
                  'pending', 'deferred', 'retryable_failed', 'delivery_unknown'
              )
            for update;
        end if;

        if v_action_id is not null then
            update public.scheduled_actions
            set due_at = case
                    when v_sequence_status = 'active' then least(due_at, now())
                    else due_at
                end,
                next_attempt_at = case
                    when v_sequence_status = 'active' then null
                    else next_attempt_at
                end,
                expected_case_version = v_case_version
            where id = v_action_id
              and status in ('pending', 'deferred', 'retryable_failed');
        elsif v_case_status = 'paused' or v_sequence_status = 'paused' then
            null;
        else
            select * into strict v_policy
            from public.followup_policy_versions
            where policy_key = v_case_policy_key
              and version = v_case_policy_version;

            select min(rce.observed_at) into strict v_original_abandoned_at
            from public.recovery_case_events rce
            where rce.recovery_case_id = v_case_id
              and rce.event_role = 'cart_abandonment';

            insert into public.followup_sequences (
                recovery_case_id,
                status,
                reason,
                policy_key,
                policy_version,
                current_step,
                max_attempts
            ) values (
                v_case_id,
                'active',
                'cart_abandonment',
                v_case_policy_key,
                v_case_policy_version,
                0,
                v_policy.max_automatic_messages
            )
            returning id into v_sequence_id;

            insert into public.scheduled_actions (
                recovery_case_id,
                followup_sequence_id,
                action_type,
                status,
                due_at,
                expires_at,
                expected_case_version,
                idempotency_key,
                policy_key,
                policy_version,
                step_key,
                anchor_type,
                anchor_subject_internal_id,
                anchor_observed_at,
                anchor_checkpoint
            ) values (
                v_case_id,
                v_sequence_id,
                'first_contact_review',
                'pending',
                now(),
                v_original_abandoned_at + v_policy.expires_after,
                v_case_version,
                'cart_recovery:reevaluate:' || p_webhook_event_id::text,
                v_case_policy_key,
                v_case_policy_version,
                'first_contact',
                'cart_abandonment',
                p_webhook_event_id,
                p_abandoned_at,
                jsonb_build_object('webhook_event_id', p_webhook_event_id)
            )
            returning id into v_action_id;
        end if;

        insert into public.conversation_events (
            recovery_case_id,
            event_type,
            actor_type,
            related_action_id,
            data
        ) values (
            v_case_id,
            'cart_abandonment_aggregated',
            'integration',
            v_action_id,
            jsonb_build_object(
                'policy_key', v_case_policy_key,
                'policy_version', v_case_policy_version,
                'reason_code', 'repeated_abandonment'
            )
        );

        return query select v_case_id, v_sequence_id, v_action_id, false;
        return;
    end if;

    insert into public.recovery_cases (
        contact_id,
        abandonment_event_id,
        source,
        external_product_id,
        product_name,
        offer_code,
        status,
        lead_stage,
        grace_expires_at,
        policy_key,
        policy_version,
        context
    ) values (
        p_contact_id,
        p_webhook_event_id,
        v_event.source,
        p_external_product_id,
        p_product_name,
        p_offer_code,
        'grace_period',
        'new',
        p_abandoned_at + v_policy.grace_period,
        p_policy_key,
        p_policy_version,
        jsonb_build_object(
            'latest_abandonment_event_id', p_webhook_event_id,
            'latest_abandonment_at', p_abandoned_at
        )
    )
    returning id into v_case_id;

    insert into public.recovery_case_events (
        recovery_case_id,
        webhook_event_id,
        event_role,
        observed_at
    ) values (
        v_case_id,
        p_webhook_event_id,
        'cart_abandonment',
        p_abandoned_at
    );

    insert into public.followup_sequences (
        recovery_case_id,
        status,
        reason,
        policy_key,
        policy_version,
        current_step,
        max_attempts
    ) values (
        v_case_id,
        'active',
        'cart_abandonment',
        p_policy_key,
        p_policy_version,
        0,
        v_policy.max_automatic_messages
    )
    returning id into v_sequence_id;

    insert into public.scheduled_actions (
        recovery_case_id,
        followup_sequence_id,
        action_type,
        status,
        due_at,
        expires_at,
        expected_case_version,
        idempotency_key,
        policy_key,
        policy_version,
        step_key,
        anchor_type,
        anchor_subject_internal_id,
        anchor_observed_at,
        anchor_checkpoint
    ) values (
        v_case_id,
        v_sequence_id,
        'first_contact_review',
        'pending',
        p_abandoned_at + v_policy.grace_period,
        p_abandoned_at + v_policy.expires_after,
        1,
        'cart_recovery:first_contact:' || v_case_id::text,
        p_policy_key,
        p_policy_version,
        'first_contact',
        'cart_abandonment',
        p_webhook_event_id,
        p_abandoned_at,
        jsonb_build_object('webhook_event_id', p_webhook_event_id)
    )
    returning id into v_action_id;

    insert into public.conversation_events (
        recovery_case_id,
        event_type,
        actor_type,
        related_action_id,
        data
    ) values (
        v_case_id,
        'cart_recovery_planned',
        'system',
        v_action_id,
        jsonb_build_object(
            'policy_key', p_policy_key,
            'policy_version', p_policy_version,
            'from_status', null,
            'to_status', 'pending',
            'reason_code', 'cart_abandonment'
        )
    );

    return query select v_case_id, v_sequence_id, v_action_id, true;
end;
$function$;

create or replace function public.reserve_followup_delivery_attempt(
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_expected_case_version bigint,
    p_expected_sequence_revision bigint,
    p_channel text,
    p_mode text,
    p_now timestamptz
)
returns setof public.followup_delivery_attempts
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_case public.recovery_cases%rowtype;
    v_sequence public.followup_sequences%rowtype;
    v_existing public.followup_delivery_attempts%rowtype;
begin
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

    if not (
        v_action.lease_owner = p_worker_id
        and v_action.lease_generation = p_lease_generation
        and v_action.lease_expires_at > p_now
        and v_action.expires_at > p_now
        and v_action.status in ('pending', 'deferred', 'retryable_failed')
        and v_case.version = p_expected_case_version
        and v_action.expected_case_version = p_expected_case_version
        and v_sequence.revision = p_expected_sequence_revision
        and v_case.status in ('grace_period', 'active')
        and v_sequence.status = 'active'
    ) then
        raise exception using
            errcode = 'P0002',
            message = 'current_action_authorization_not_found';
    end if;

    select * into v_existing
    from public.followup_delivery_attempts
    where action_id = p_action_id
      and lease_generation = p_lease_generation
    for update;

    if found then
        if v_existing.idempotency_key is distinct from v_action.idempotency_key
           or v_existing.attempt_number is distinct from v_action.execution_attempt_count
           or v_existing.channel is distinct from p_channel
           or v_existing.mode is distinct from p_mode
           or v_existing.expected_case_version is distinct from p_expected_case_version
           or v_existing.expected_sequence_revision is distinct from p_expected_sequence_revision then
            raise exception using
                errcode = '22000',
                message = 'delivery_attempt_already_reserved_differently';
        end if;

        return next v_existing;
        return;
    end if;

    update public.scheduled_actions
    set execution_attempt_count = execution_attempt_count + 1
    where id = p_action_id
    returning * into v_action;

    return query
    insert into public.followup_delivery_attempts (
        action_id,
        idempotency_key,
        attempt_number,
        channel,
        mode,
        phase,
        started_at,
        lease_generation,
        expected_case_version,
        expected_sequence_revision
    ) values (
        v_action.id,
        v_action.idempotency_key,
        v_action.execution_attempt_count,
        p_channel,
        p_mode,
        'reserved',
        p_now,
        p_lease_generation,
        p_expected_case_version,
        p_expected_sequence_revision
    )
    returning *;
end;
$function$;

create or replace function public.mark_followup_request_started(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns setof public.followup_delivery_attempts
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_sequence public.followup_sequences%rowtype;
begin
    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;

    select fda.* into strict v_attempt
    from public.followup_delivery_attempts fda
    where fda.id = p_attempt_id
      and fda.action_id = p_action_id
      and fda.lease_generation = p_lease_generation;

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
      and (fda.phase = 'reserved' or fda.phase = 'request_started')
    for update;

    if not (
        v_case.version = v_attempt.expected_case_version
        and v_sequence.revision = v_attempt.expected_sequence_revision
        and v_case.status in ('grace_period', 'active')
        and v_sequence.status = 'active'
    ) then
        raise exception using
            errcode = '55000',
            message = 'authoritative_state_changed_before_request';
    end if;

    if not (
        v_action.lease_owner = p_worker_id
        and v_action.lease_generation = p_lease_generation
        and v_action.lease_expires_at > p_now
        and v_action.expires_at > p_now
        and v_action.status in ('pending', 'deferred', 'retryable_failed')
    ) then
        raise exception using errcode = 'P0002', message = 'current_action_lease_not_found';
    end if;

    if v_attempt.phase = 'request_started' then
        return next v_attempt;
        return;
    end if;

    return query
    update public.followup_delivery_attempts
    set phase = 'request_started',
        request_started_at = p_now
    where id = p_attempt_id
    returning *;
end;
$function$;

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
        and v_action.status in (
            'pending', 'deferred', 'retryable_failed', 'delivery_unknown'
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
        end if;

        if v_sequence.automatic_messages_accepted < v_sequence.max_attempts
           and v_next_step is not null
           and p_now + v_next_delay < v_action.expires_at then
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
                anchor_type,
                anchor_subject_internal_id,
                anchor_observed_at,
                anchor_checkpoint
            ) values (
                v_sequence.id,
                v_action.recovery_case_id,
                'no_reply_review',
                'pending',
                p_now + v_next_delay,
                v_action.expires_at,
                v_case_version,
                v_action.max_execution_retries,
                'cart_recovery:' || (v_next_step ->> 'step_key') || ':' || v_sequence.id::text,
                v_action.policy_key,
                v_action.policy_version,
                v_next_step ->> 'step_key',
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
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_sequence public.followup_sequences%rowtype;
    v_authoritative_current boolean;
begin
    if p_resolution not in ('accepted_by_chatwoot', 'not_applied', 'escalated') then
        raise exception using errcode = '22023', message = 'invalid_reconciliation_resolution';
    end if;
    if p_resolution = 'accepted_by_chatwoot'
       and (p_remote_message_id is null or btrim(p_remote_message_id) = '') then
        raise exception using errcode = '22023', message = 'remote_message_id_required';
    end if;
    if p_resolution = 'accepted_by_chatwoot' and p_accepted_message_id is null then
        raise exception using errcode = '22023', message = 'accepted_message_id_required';
    end if;
    if p_resolution = 'not_applied' and p_next_attempt_at is null then
        raise exception using errcode = '22023', message = 'next_attempt_at_required';
    end if;

    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;

    select fda.* into strict v_attempt
    from public.followup_delivery_attempts fda
    where fda.id = p_attempt_id
      and fda.action_id = p_action_id
      and fda.lease_generation = p_lease_generation;

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

    if v_attempt.reconciliation_resolution is not null then
        if v_attempt.reconciliation_resolution is distinct from p_resolution
           or (
               p_resolution = 'accepted_by_chatwoot'
               and (
                   v_attempt.remote_message_id is distinct from p_remote_message_id
                   or v_attempt.accepted_message_id is distinct from p_accepted_message_id
                   or v_attempt.reason_code is distinct from p_reason_code
               )
           )
           or (
               p_resolution = 'not_applied'
               and (
                   v_attempt.reconciliation_next_attempt_at is distinct from p_next_attempt_at
                   or v_attempt.reason_code is distinct from p_reason_code
               )
           )
           or (
               p_resolution = 'escalated'
               and v_attempt.reason_code is distinct from coalesce(
                   p_reason_code, 'reconciliation_inconclusive'
               )
           ) then
            raise exception using
                errcode = '22000',
                message = 'delivery_attempt_already_reconciled_differently';
        end if;

        return next v_action;
        return;
    end if;

    if v_attempt.phase <> 'completed'
       or v_attempt.outcome <> 'delivery_unknown' then
        raise exception using
            errcode = '55000',
            message = 'delivery_attempt_not_pending_reconciliation';
    end if;

    v_authoritative_current := (
        v_case.version = v_attempt.expected_case_version
        and v_sequence.revision = v_attempt.expected_sequence_revision
        and v_case.status in ('grace_period', 'active')
        and v_sequence.status = 'active'
        and v_action.status in (
            'pending', 'deferred', 'retryable_failed', 'delivery_unknown'
        )
    );

    if p_resolution = 'not_applied' and not v_authoritative_current then
        raise exception using
            errcode = '55000',
            message = 'authoritative_state_requires_escalation';
    end if;

    if p_resolution <> 'escalated'
       and (
           v_attempt.reconciliation_deadline is null
           or p_now >= v_attempt.reconciliation_deadline
       ) then
        raise exception using
            errcode = '55000',
            message = 'reconciliation_window_expired';
    end if;

    if p_resolution = 'not_applied' and (
        p_next_attempt_at <= p_now
        or v_action.expires_at is null
        or p_next_attempt_at >= v_action.expires_at
        or v_action.execution_attempt_count > v_action.max_execution_retries
    ) then
        raise exception using
            errcode = '55000',
            message = 'not_applied_retry_not_permitted';
    end if;

    if p_resolution = 'escalated' then
        if v_attempt.reconciliation_deadline is null
           or p_now < v_attempt.reconciliation_deadline then
            raise exception using
                errcode = '55000',
                message = 'reconciliation_window_not_expired';
        end if;

        update public.followup_delivery_attempts
        set reconciliation_resolution = 'escalated',
            reconciled_at = p_now,
            reason_code = coalesce(p_reason_code, 'reconciliation_inconclusive')
        where id = p_attempt_id;

        update public.scheduled_actions
        set terminal_reason = concat_ws(
                ':',
                nullif(terminal_reason, ''),
                coalesce(p_reason_code, 'delivery_unknown_escalated')
            ),
            lease_owner = null,
            lease_expires_at = null
        where id = p_action_id
        returning * into v_action;

        update public.followup_sequences
        set status = 'paused',
            revision = revision + 1
        where id = v_sequence.id
          and status = 'active';

        update public.recovery_cases
        set status = 'paused',
            version = version + 1
        where id = v_case.id
          and status in ('grace_period', 'active');
    else
        update public.followup_delivery_attempts
        set phase = 'request_started',
            outcome = null,
            remote_message_id = null,
            accepted_message_id = null,
            accepted_at = null,
            reason_code = null,
            reconciliation_deadline = null
        where id = p_attempt_id;

        select * into strict v_action
        from public.finalize_followup_delivery_attempt(
            p_action_id,
            p_attempt_id,
            'reconciliation',
            p_lease_generation,
            case
                when p_resolution = 'accepted_by_chatwoot' then 'accepted_by_chatwoot'
                else 'rejected'
            end,
            p_remote_message_id,
            p_accepted_message_id,
            p_reason_code,
            p_next_attempt_at,
            null,
            p_now
        );

        update public.followup_delivery_attempts
        set reconciliation_resolution = p_resolution,
            reconciliation_next_attempt_at = case
                when p_resolution = 'not_applied' then p_next_attempt_at
                else null
            end,
            reconciled_at = p_now
        where id = p_attempt_id;
    end if;

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
            'policy_key', v_action.policy_key,
            'policy_version', v_action.policy_version,
            'from_status', 'delivery_unknown',
            'to_status', v_action.status,
            'reason_code', p_reason_code,
            'resolution', p_resolution,
            'attempt_id', p_attempt_id,
            'lease_generation', p_lease_generation
        )
    );

    return next v_action;
end;
$function$;

create or replace function public.claim_due_followup_actions(
    p_worker_id text,
    p_now timestamptz,
    p_lease_duration interval,
    p_batch_size integer
)
returns setof public.scheduled_actions
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if p_worker_id is null or btrim(p_worker_id) = '' then
        raise exception using errcode = '22023', message = 'worker_id_required';
    end if;
    if p_lease_duration is null or p_lease_duration <= interval '0 seconds' then
        raise exception using errcode = '22023', message = 'positive_lease_required';
    end if;
    if p_batch_size is null or p_batch_size < 1 or p_batch_size > 100 then
        raise exception using errcode = '22023', message = 'invalid_batch_size';
    end if;

    return query
    with due as (
        select sa.id, sa.lease_generation
        from public.scheduled_actions sa
        where sa.status in ('pending', 'deferred', 'retryable_failed')
          and coalesce(sa.next_attempt_at, sa.due_at) <= p_now
          and sa.expires_at > p_now
          and (sa.lease_expires_at is null or sa.lease_expires_at <= p_now)
          and not exists (
              select 1
              from public.followup_delivery_attempts fda
              where fda.action_id = sa.id
                and fda.phase = 'request_started'
          )
        order by coalesce(sa.next_attempt_at, sa.due_at), sa.id
        for update skip locked
        limit p_batch_size
    ), claimed as (
        update public.scheduled_actions sa
        set lease_owner = p_worker_id,
            lease_generation = due.lease_generation + 1,
            lease_expires_at = p_now + p_lease_duration,
            claimed_at = p_now
        from due
        where sa.id = due.id
        returning sa.*
    ), audited as (
        insert into public.conversation_events (
            recovery_case_id,
            event_type,
            actor_type,
            related_action_id,
            data
        )
        select
            claimed.recovery_case_id,
            'followup_action_claimed',
            'system',
            claimed.id,
            jsonb_build_object(
                'policy_key', claimed.policy_key,
                'policy_version', claimed.policy_version,
                'worker_id', p_worker_id,
                'lease_generation', claimed.lease_generation,
                'claimed_at', p_now,
                'lease_expires_at', claimed.lease_expires_at
            )
        from claimed
        returning related_action_id
    )
    select claimed.*
    from claimed
    join audited on audited.related_action_id = claimed.id;
end;
$function$;

revoke all on public.followup_policy_versions from public;
revoke all on public.contact_authorizations from public;
revoke all on public.recovery_case_events from public;
revoke all on public.followup_delivery_attempts from public;
revoke execute on function public.protect_scheduled_action_identity() from public;

revoke execute on function public.plan_cart_recovery(
    uuid, uuid, text, text, text, text, integer, timestamptz
) from public;
revoke execute on function public.claim_due_followup_actions(
    text, timestamptz, interval, integer
) from public;
revoke execute on function public.reserve_followup_delivery_attempt(
    uuid, text, bigint, bigint, bigint, text, text, timestamptz
) from public;
revoke execute on function public.mark_followup_request_started(
    uuid, uuid, text, bigint, timestamptz
) from public;
revoke execute on function public.finalize_followup_delivery_attempt(
    uuid, uuid, text, bigint, text, text, uuid, text, timestamptz,
    timestamptz, timestamptz
) from public;
revoke execute on function public.reconcile_followup_delivery_attempt(
    uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz
) from public;

do $privileges$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on public.followup_policy_versions, public.contact_authorizations, public.recovery_case_events, public.followup_delivery_attempts from anon';
        execute 'revoke execute on function public.protect_scheduled_action_identity() from anon';
        execute 'revoke execute on function public.plan_cart_recovery(uuid, uuid, text, text, text, text, integer, timestamptz), public.claim_due_followup_actions(text, timestamptz, interval, integer), public.reserve_followup_delivery_attempt(uuid, text, bigint, bigint, bigint, text, text, timestamptz), public.mark_followup_request_started(uuid, uuid, text, bigint, timestamptz), public.finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz), public.reconcile_followup_delivery_attempt(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on public.followup_policy_versions, public.contact_authorizations, public.recovery_case_events, public.followup_delivery_attempts from authenticated';
        execute 'revoke execute on function public.protect_scheduled_action_identity() from authenticated';
        execute 'revoke execute on function public.plan_cart_recovery(uuid, uuid, text, text, text, text, integer, timestamptz), public.claim_due_followup_actions(text, timestamptz, interval, integer), public.reserve_followup_delivery_attempt(uuid, text, bigint, bigint, bigint, text, text, timestamptz), public.mark_followup_request_started(uuid, uuid, text, bigint, timestamptz), public.finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz), public.reconcile_followup_delivery_attempt(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'grant all on public.followup_policy_versions, public.contact_authorizations, public.recovery_case_events, public.followup_delivery_attempts to service_role';
        execute 'grant execute on function public.plan_cart_recovery(uuid, uuid, text, text, text, text, integer, timestamptz) to service_role';
        execute 'grant execute on function public.claim_due_followup_actions(text, timestamptz, interval, integer) to service_role';
        execute 'grant execute on function public.reserve_followup_delivery_attempt(uuid, text, bigint, bigint, bigint, text, text, timestamptz) to service_role';
        execute 'grant execute on function public.mark_followup_request_started(uuid, uuid, text, bigint, timestamptz) to service_role';
        execute 'grant execute on function public.finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) to service_role';
        execute 'grant execute on function public.reconcile_followup_delivery_attempt(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) to service_role';
    end if;
end;
$privileges$;

commit;
