-- Durable, fail-closed payment-link attribution for Chatwoot recovery replies.
-- The canonical checkout URL remains an opaque string. Only an allowlisted
-- tracking pair produced by the bridge may be appended.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- A precheckout event is the immutable origin of one recovery sequence. A later
-- precheckout closes the former sequence and creates a new row instead of
-- rewriting source_submission_id on the existing row.
alter table public.hotmart_abandonment_reevaluations
    drop constraint hotmart_abandonment_reevaluations_outcome_check,
    add constraint hotmart_abandonment_reevaluations_outcome_check
        check (outcome in (
            'cancelled_purchased',
            'blocked_not_authorized',
            'blocked_contact_binding_missing',
            'cancelled_intent_changed',
            'superseded_by_provider_event',
            'superseded_by_newer_precheckout',
            'blocked_contact',
            'blocked_identity',
            'blocked_handoff',
            'budget_consumed',
            'command_reserved'
        ));

do $remove_mutable_sequence_identity$
declare
    v_function regprocedure :=
        to_regprocedure('public.protect_hotmart_abandonment_reevaluation()');
    v_definition text;
    v_old text := $old$    if old.source_kind = 'precheckout_intent'
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

$old$;
begin
    if v_function is null then
        raise exception 'payment_link_sequence_guard_missing';
    end if;
    select pg_get_functiondef(v_function) into strict v_definition;
    if strpos(v_definition, v_old) = 0 then
        raise exception 'payment_link_sequence_guard_shape_unexpected';
    end if;
    v_definition := replace(v_definition, v_old, '');
    execute v_definition;
end;
$remove_mutable_sequence_identity$;

do $replace_precheckout_sequence_coalescing$
declare
    v_function regprocedure := to_regprocedure(
        'public.schedule_precheckout_first_touch_reevaluation(uuid,uuid)'
    );
    v_definition text;
    v_old text := $old$    if found then
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
$old$;
    v_new text := $new$    if found then
        if v_observed_at <= v_existing.observed_at then
            return query select 'superseded_by_newer_precheckout'::text,
                v_existing.id, false;
            return;
        end if;
        update public.hotmart_abandonment_reevaluations
        set status = 'completed',
            outcome = 'superseded_by_newer_precheckout',
            completed_at = clock_timestamp(),
            updated_at = clock_timestamp()
        where id = v_existing.id;
    end if;
$new$;
begin
    if v_function is null then
        raise exception 'payment_link_precheckout_scheduler_missing';
    end if;
    select pg_get_functiondef(v_function) into strict v_definition;
    if strpos(v_definition, v_old) = 0 then
        raise exception 'payment_link_precheckout_scheduler_shape_unexpected';
    end if;
    v_definition := replace(v_definition, v_old, v_new);
    execute v_definition;
end;
$replace_precheckout_sequence_coalescing$;

alter table public.purchase_intent_submissions
    add constraint purchase_intent_submissions_intent_submission_key
    unique (purchase_intent_id, submission_id);

create table public.payment_link_bindings (
    id uuid primary key default gen_random_uuid(),
    source_reevaluation_id uuid not null unique
        references public.hotmart_abandonment_reevaluations(id) on delete restrict,
    purchase_intent_id uuid not null
        references public.purchase_intents(id) on delete restrict,
    source_submission_id uuid not null unique
        references public.precheckout_submissions(id) on delete restrict,
    sequence_origin_event_ulid text not null unique
        check (sequence_origin_event_ulid ~ '^[0-9A-HJKMNP-TV-Z]{26}$'),
    checkout_url_original text not null,
    checkout_url_final text not null,
    tracking_field text not null check (tracking_field in ('src', 'xcod')),
    tracking_prefix text not null
        check (tracking_prefix ~ '^[a-z0-9][a-z0-9-]{0,30}-$'),
    tracking_value text not null unique,
    created_at timestamptz not null default clock_timestamp(),
    check (checkout_url_original ~ '^https://pay[.]hotmart[.]com/'),
    foreign key (purchase_intent_id, source_submission_id)
        references public.purchase_intent_submissions(
            purchase_intent_id, submission_id
        ) on delete restrict,
    check (tracking_value = tracking_prefix || sequence_origin_event_ulid),
    check (checkout_url_final = checkout_url_original || '&' || tracking_field || '=' || tracking_value)
);

create table public.payment_link_send_commands (
    id uuid primary key default gen_random_uuid(),
    binding_id uuid not null references public.payment_link_bindings(id) on delete restrict,
    commercial_case_id uuid not null references public.commercial_cases(id) on delete restrict,
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    chatwoot_conversation_id bigint not null check (chatwoot_conversation_id > 0),
    trigger_external_message_id text not null check (trigger_external_message_id ~ '^[1-9][0-9]*$'),
    status text not null default 'request_started'
        check (status in ('request_started', 'accepted_by_chatwoot', 'delivery_unknown')),
    chatwoot_message_id bigint,
    failure_code text,
    created_at timestamptz not null default clock_timestamp(),
    finalized_at timestamptz,
    unique (
        chatwoot_account_id,
        chatwoot_inbox_id,
        chatwoot_conversation_id,
        trigger_external_message_id
    ),
    check (
        (status = 'request_started' and chatwoot_message_id is null and failure_code is null and finalized_at is null)
        or (status = 'accepted_by_chatwoot' and chatwoot_message_id > 0 and finalized_at is not null)
        or (status = 'delivery_unknown' and failure_code is not null and finalized_at is not null)
    )
);

alter table public.payment_link_bindings enable row level security;
alter table public.payment_link_send_commands enable row level security;

create function public.protect_payment_link_binding()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = 'payment_link_binding_immutable';
end;
$function$;

create trigger payment_link_bindings_immutable
before update or delete on public.payment_link_bindings
for each row execute function public.protect_payment_link_binding();

create function public.protect_payment_link_send_command()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE'
       or old.id is distinct from new.id
       or old.binding_id is distinct from new.binding_id
       or old.commercial_case_id is distinct from new.commercial_case_id
       or old.chatwoot_account_id is distinct from new.chatwoot_account_id
       or old.chatwoot_inbox_id is distinct from new.chatwoot_inbox_id
       or old.chatwoot_conversation_id is distinct from new.chatwoot_conversation_id
       or old.trigger_external_message_id is distinct from new.trigger_external_message_id
       or old.created_at is distinct from new.created_at
       or not (
           (old.status = 'request_started' and new.status in ('accepted_by_chatwoot', 'delivery_unknown'))
           or (old.status = 'delivery_unknown' and new.status = 'accepted_by_chatwoot')
       ) then
        raise exception using errcode = '55000', message = 'payment_link_send_command_immutable';
    end if;
    return new;
end;
$function$;

create trigger payment_link_send_commands_immutable
before update or delete on public.payment_link_send_commands
for each row execute function public.protect_payment_link_send_command();

create function public.get_chatwoot_payment_link_candidate(
    p_commercial_case_id uuid,
    p_external_user_id text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_max_age_seconds integer,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    source_reevaluation_id uuid,
    purchase_intent_id uuid,
    source_submission_id uuid,
    sequence_origin_event_ulid text,
    canonical_checkout_url text,
    submitted_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_conversation public.conversations%rowtype;
    v_identity public.channel_identities%rowtype;
    v_scope public.inbound_commercial_scope_versions%rowtype;
    v_command public.johanna_abandonment_one_shot_commands%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_reevaluation public.hotmart_abandonment_reevaluations%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_latest_submission_id uuid;
    v_url text;
    v_submitted_at timestamptz;
begin
    if p_external_user_id is null or p_external_user_id !~ '^[1-9][0-9]*$'
       or p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id <= 0
       or p_max_age_seconds is null or p_max_age_seconds <= 0 or p_max_age_seconds > 2592000
       or p_now is null then
        return query select 'invalid_request'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select c.* into v_case
    from public.commercial_cases c
    where c.id = p_commercial_case_id
      and c.case_kind = 'inbound_sales'
    for update;
    if not found
       or v_case.status <> 'active'
       or v_case.automation_status not in ('draft_only', 'enabled') then
        return query select 'blocked_case'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select scope.* into v_scope
    from public.inbound_commercial_scope_versions scope
    where scope.scope_key = v_case.inbound_scope_key
      and scope.version = v_case.inbound_scope_version
      and scope.status = 'published'
      and scope.tenant_key = v_case.tenant_ref
      and scope.chatwoot_account_id = p_chatwoot_account_id
      and scope.chatwoot_inbox_id = p_chatwoot_inbox_id
      and lower(scope.external_product_id) = lower(v_case.product_ref)
      and scope.offer_code = v_case.offer_ref
    for share;
    if not found then
        return query select 'blocked_scope'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select c.* into v_contact from public.contacts c where c.id = v_case.contact_id for update;
    if not found
       or v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or v_contact.lifecycle_status = 'do_not_contact' then
        return query select 'blocked_contact'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;
    if public.has_chatwoot_opt_out_stop(
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        p_chatwoot_conversation_id,
        p_external_user_id
    ) then
        return query select 'blocked_opt_out'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select c.* into v_conversation
    from public.conversations c
    where c.id = v_case.conversation_id
      and c.contact_id = v_case.contact_id
      and c.channel_identity_id = v_case.selected_channel_identity_id
    for update;
    if not found
       or v_conversation.human_takeover
       or v_conversation.status in ('snoozed', 'paused_human', 'completed', 'closed', 'blocked')
       or v_conversation.automation_status not in ('draft_only', 'enabled')
       or v_conversation.commercial_context #>> '{chatwoot_conversation_id}' <> p_chatwoot_conversation_id::text then
        return query select 'blocked_conversation'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select i.* into v_identity
    from public.channel_identities i
    where i.id = v_case.selected_channel_identity_id
      and i.external_user_id = p_external_user_id
      and i.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and i.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and i.external_conversation_id = p_chatwoot_conversation_id::text
      and i.identity_status = 'active'
    for update;
    if not found then
        return query select 'blocked_identity'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select cmd.* into v_command
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.chatwoot_account_id = p_chatwoot_account_id
      and cmd.chatwoot_inbox_id = p_chatwoot_inbox_id
      and cmd.chatwoot_conversation_id = p_chatwoot_conversation_id
      and cmd.status = 'accepted_by_chatwoot'
      and cmd.source_reevaluation_id is not null
    order by cmd.created_at desc, cmd.id desc
    limit 1
    for update;
    if not found then
        return query select 'missing_precheckout_sequence'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select i.* into v_intent
    from public.purchase_intents i
    where i.id = v_command.purchase_intent_id
    for update;
    if not found or v_intent.lifecycle_state <> 'waiting_for_purchase' then
        return query select 'purchase_not_open'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select r.* into v_reevaluation
    from public.hotmart_abandonment_reevaluations r
    where r.id = v_command.source_reevaluation_id
      and r.purchase_intent_id = v_intent.id
      and r.source_kind = 'precheckout_intent'
      and r.source_submission_id is not null
    for update;
    if not found then
        return query select 'missing_precheckout_sequence'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    select s.* into v_submission
    from public.precheckout_submissions s
    join public.purchase_intent_submissions link
      on link.submission_id = s.id
     and link.purchase_intent_id = v_intent.id
    where s.id = v_reevaluation.source_submission_id
    for update of s;
    if not found or v_submission.external_submission_id !~ '^[0-9A-HJKMNP-TV-Z]{26}$' then
        return query select 'invalid_precheckout_sequence'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    begin
        select s.id into strict v_latest_submission_id
        from public.purchase_intent_submissions link
        join public.precheckout_submissions s on s.id = link.submission_id
        where link.purchase_intent_id = v_intent.id
        order by
            (s.canonical_payload #>> '{submitted_at}')::timestamptz desc,
            link.ordinal desc,
            s.id desc
        limit 1;
    exception when others then
        return query select 'invalid_precheckout_sequence'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end;
    if v_latest_submission_id is distinct from v_submission.id then
        return query select 'precheckout_sequence_not_latest'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    if v_intent.tenant_ref is distinct from v_scope.tenant_key
       or lower(v_intent.product_ref) is distinct from lower(v_scope.external_product_id)
       or v_intent.offer_ref is distinct from v_scope.offer_code
       or v_intent.normalized_phone is distinct from p_external_user_id then
        return query select 'precheckout_scope_mismatch'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;

    v_url := v_submission.canonical_payload #>> '{commerce,checkout_url}';
    begin
        v_submitted_at := (v_submission.canonical_payload #>> '{submitted_at}')::timestamptz;
    exception when others then
        v_submitted_at := null;
    end;
    if v_url is null or v_url !~ '^https://pay[.]hotmart[.]com/.+[?].+'
       or v_submitted_at is null then
        return query select 'invalid_checkout_url'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;
    if p_now < v_submitted_at then
        return query select 'precheckout_from_future'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, null::timestamptz;
        return;
    end if;
    if p_now > v_submitted_at + make_interval(secs => p_max_age_seconds) then
        return query select 'checkout_url_stale'::text, null::uuid, null::uuid, null::uuid, null::text, null::text, v_submitted_at;
        return;
    end if;

    return query select 'available'::text, v_reevaluation.id, v_intent.id, v_submission.id,
        v_submission.external_submission_id, v_url, v_submitted_at;
end;
$function$;

create function public.prepare_chatwoot_payment_link_send(
    p_commercial_case_id uuid,
    p_external_user_id text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_trigger_external_message_id text,
    p_max_age_seconds integer,
    p_source_reevaluation_id uuid,
    p_source_submission_id uuid,
    p_checkout_url_original text,
    p_checkout_url_final text,
    p_tracking_field text,
    p_tracking_value text,
    p_tracking_prefix text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    send_command_id uuid,
    binding_id uuid,
    checkout_url_final text,
    tracking_field text,
    tracking_value text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_candidate record;
    v_binding public.payment_link_bindings%rowtype;
    v_command public.payment_link_send_commands%rowtype;
begin
    if p_trigger_external_message_id is null or p_trigger_external_message_id !~ '^[1-9][0-9]*$'
       or p_tracking_field is null or not (p_tracking_field = any(array['src', 'xcod']))
       or p_tracking_value is null or length(p_tracking_value) not between 2 and 64
       or p_tracking_prefix is null or p_tracking_prefix !~ '^[a-z0-9][a-z0-9-]{0,30}-$'
       or p_checkout_url_original is null or p_checkout_url_final is null
       or p_checkout_url_final <> p_checkout_url_original || '&' || p_tracking_field || '=' || p_tracking_value then
        return query select 'invalid_request'::text, null::uuid, null::uuid, null::text, null::text, null::text;
        return;
    end if;

    select * into v_candidate
    from public.get_chatwoot_payment_link_candidate(
        p_commercial_case_id, p_external_user_id, p_chatwoot_account_id,
        p_chatwoot_inbox_id, p_chatwoot_conversation_id, p_max_age_seconds, p_now
    );
    if v_candidate.outcome <> 'available' then
        return query select v_candidate.outcome, null::uuid, null::uuid, null::text, null::text, null::text;
        return;
    end if;
    if v_candidate.source_reevaluation_id <> p_source_reevaluation_id
       or v_candidate.source_submission_id <> p_source_submission_id
       or v_candidate.canonical_checkout_url <> p_checkout_url_original
       or p_tracking_value <> p_tracking_prefix || v_candidate.sequence_origin_event_ulid
       or (
           p_tracking_field = 'src'
           and p_checkout_url_original ~ '(^|[?&])src([=&]|$)'
       )
       or (
           p_tracking_field = 'xcod'
           and (
               p_checkout_url_original !~ '(^|[?&])src([=&]|$)'
               or p_checkout_url_original ~ '(^|[?&])xcod([=&]|$)'
           )
       ) then
        return query select 'candidate_changed'::text, null::uuid, null::uuid, null::text, null::text, null::text;
        return;
    end if;

    select c.* into v_command
    from public.payment_link_send_commands c
    where c.chatwoot_account_id = p_chatwoot_account_id
      and c.chatwoot_inbox_id = p_chatwoot_inbox_id
      and c.chatwoot_conversation_id = p_chatwoot_conversation_id
      and c.trigger_external_message_id = p_trigger_external_message_id
    for update;
    if found then
        return query select
            case when v_command.status = 'accepted_by_chatwoot' then 'already_accepted' else 'delivery_unknown' end,
            v_command.id, v_command.binding_id, null::text, null::text, null::text;
        return;
    end if;

    insert into public.payment_link_bindings (
        source_reevaluation_id, purchase_intent_id, source_submission_id,
        sequence_origin_event_ulid, checkout_url_original, checkout_url_final,
        tracking_field, tracking_prefix, tracking_value
    ) values (
        v_candidate.source_reevaluation_id, v_candidate.purchase_intent_id,
        v_candidate.source_submission_id, v_candidate.sequence_origin_event_ulid,
        p_checkout_url_original, p_checkout_url_final, p_tracking_field,
        p_tracking_prefix, p_tracking_value
    ) on conflict (source_submission_id) do nothing
    returning * into v_binding;

    if v_binding.id is null then
        select b.* into strict v_binding
        from public.payment_link_bindings b
        where b.source_submission_id = v_candidate.source_submission_id
        for share;
        if v_binding.source_reevaluation_id <> v_candidate.source_reevaluation_id
           or v_binding.checkout_url_original <> p_checkout_url_original
           or v_binding.checkout_url_final <> p_checkout_url_final
           or v_binding.tracking_field <> p_tracking_field
           or v_binding.tracking_prefix <> p_tracking_prefix
           or v_binding.tracking_value <> p_tracking_value then
            return query select 'binding_conflict'::text, null::uuid, null::uuid, null::text, null::text, null::text;
            return;
        end if;
    end if;

    insert into public.payment_link_send_commands (
        binding_id, commercial_case_id, chatwoot_account_id, chatwoot_inbox_id,
        chatwoot_conversation_id, trigger_external_message_id
    ) values (
        v_binding.id, p_commercial_case_id, p_chatwoot_account_id, p_chatwoot_inbox_id,
        p_chatwoot_conversation_id, p_trigger_external_message_id
    ) returning * into v_command;

    return query select 'request_started'::text, v_command.id, v_binding.id,
        v_binding.checkout_url_final, v_binding.tracking_field, v_binding.tracking_value;
end;
$function$;

create function public.finalize_chatwoot_payment_link_send(
    p_send_command_id uuid,
    p_status text,
    p_chatwoot_message_id bigint default null,
    p_failure_code text default null,
    p_now timestamptz default clock_timestamp()
)
returns table (outcome text, send_command_id uuid, status text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_command public.payment_link_send_commands%rowtype;
begin
    select c.* into v_command
    from public.payment_link_send_commands c
    where c.id = p_send_command_id
    for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'payment_link_send_command_not_found';
    end if;
    if v_command.status = 'delivery_unknown'
       and p_status = 'accepted_by_chatwoot'
       and p_chatwoot_message_id is not null
       and p_chatwoot_message_id > 0
       and p_failure_code is null then
        update public.payment_link_send_commands
        set status = p_status,
            chatwoot_message_id = p_chatwoot_message_id,
            finalized_at = p_now
        where id = v_command.id;
        return query select 'reconciled'::text, v_command.id, p_status;
        return;
    end if;
    if v_command.status <> 'request_started' then
        return query select 'already_finalized'::text, v_command.id, v_command.status;
        return;
    end if;
    if p_status = 'accepted_by_chatwoot' and p_chatwoot_message_id is not null and p_chatwoot_message_id > 0 and p_failure_code is null then
        update public.payment_link_send_commands
        set status = p_status, chatwoot_message_id = p_chatwoot_message_id, finalized_at = p_now
        where id = v_command.id;
    elsif p_status = 'delivery_unknown' and nullif(btrim(p_failure_code), '') is not null and p_chatwoot_message_id is null then
        update public.payment_link_send_commands
        set status = p_status, failure_code = p_failure_code, finalized_at = p_now
        where id = v_command.id;
    else
        raise exception using errcode = '22023', message = 'payment_link_finalize_invalid';
    end if;
    return query select 'finalized'::text, v_command.id, p_status;
end;
$function$;

revoke all on public.payment_link_bindings from public;
revoke all on public.payment_link_send_commands from public;
revoke all on function public.protect_payment_link_binding() from public;
revoke all on function public.protect_payment_link_send_command() from public;
revoke all on function public.get_chatwoot_payment_link_candidate(uuid, text, bigint, bigint, bigint, integer, timestamptz) from public;
revoke all on function public.prepare_chatwoot_payment_link_send(uuid, text, bigint, bigint, bigint, text, integer, uuid, uuid, text, text, text, text, text, timestamptz) from public;
revoke all on function public.finalize_chatwoot_payment_link_send(uuid, text, bigint, text, timestamptz) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on public.payment_link_bindings from anon;
        revoke all on public.payment_link_send_commands from anon;
        revoke all on function public.protect_payment_link_binding() from anon;
        revoke all on function public.protect_payment_link_send_command() from anon;
        revoke all on function public.get_chatwoot_payment_link_candidate(uuid, text, bigint, bigint, bigint, integer, timestamptz) from anon;
        revoke all on function public.prepare_chatwoot_payment_link_send(uuid, text, bigint, bigint, bigint, text, integer, uuid, uuid, text, text, text, text, text, timestamptz) from anon;
        revoke all on function public.finalize_chatwoot_payment_link_send(uuid, text, bigint, text, timestamptz) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on public.payment_link_bindings from authenticated;
        revoke all on public.payment_link_send_commands from authenticated;
        revoke all on function public.protect_payment_link_binding() from authenticated;
        revoke all on function public.protect_payment_link_send_command() from authenticated;
        revoke all on function public.get_chatwoot_payment_link_candidate(uuid, text, bigint, bigint, bigint, integer, timestamptz) from authenticated;
        revoke all on function public.prepare_chatwoot_payment_link_send(uuid, text, bigint, bigint, bigint, text, integer, uuid, uuid, text, text, text, text, text, timestamptz) from authenticated;
        revoke all on function public.finalize_chatwoot_payment_link_send(uuid, text, bigint, text, timestamptz) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on public.payment_link_bindings from service_role;
        revoke all on public.payment_link_send_commands from service_role;
        revoke all on function public.protect_payment_link_binding() from service_role;
        revoke all on function public.protect_payment_link_send_command() from service_role;
        grant execute on function public.get_chatwoot_payment_link_candidate(uuid, text, bigint, bigint, bigint, integer, timestamptz) to service_role;
        grant execute on function public.prepare_chatwoot_payment_link_send(uuid, text, bigint, bigint, bigint, text, integer, uuid, uuid, text, text, text, text, text, timestamptz) to service_role;
        grant execute on function public.finalize_chatwoot_payment_link_send(uuid, text, bigint, text, timestamptz) to service_role;
    end if;
end;
$roles$;

commit;
