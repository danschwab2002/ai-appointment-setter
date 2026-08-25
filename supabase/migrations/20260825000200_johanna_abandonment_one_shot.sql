-- Controlled, single-budget WABA template command for Johanna's first real E2E.

begin;

create table public.johanna_abandonment_one_shot_commands (
    id uuid primary key default gen_random_uuid(),
    command_key text not null unique check (command_key ~ '^[a-z0-9:_-]{1,200}$'),
    semantic_fingerprint text not null check (semantic_fingerprint ~ '^[0-9a-f]{64}$'),
    rollout_scope text not null unique
        check (rollout_scope = 'johanna-abandonment-template-e2e-v1'),
    purchase_intent_id uuid not null unique
        references public.purchase_intents(id) on delete restrict,
    scope_key text not null,
    scope_version integer not null check (scope_version = 1),
    runtime_generation bigint not null check (runtime_generation = 0),
    chatwoot_account_id bigint not null check (chatwoot_account_id = 1),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id = 9),
    target_phone text not null check (target_phone ~ '^[1-9][0-9]{7,14}$'),
    template_name text not null
        check (template_name = 'johanna_carrito_abandonado_01'),
    template_language text not null check (template_language = 'es_EC'),
    template_category text not null check (template_category = 'MARKETING'),
    copy_version text not null
        check (copy_version = 'johanna-abandonment-one-shot-v1'),
    max_messages integer not null default 1 check (max_messages = 1),
    followups_allowed integer not null default 0 check (followups_allowed = 0),
    status text not null default 'request_started'
        check (status in ('request_started', 'accepted_by_chatwoot', 'delivery_unknown')),
    chatwoot_conversation_id bigint,
    chatwoot_message_id bigint,
    failure_code text,
    created_at timestamptz not null default clock_timestamp(),
    finalized_at timestamptz,
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version) on delete restrict,
    check (
        (status = 'request_started'
            and chatwoot_conversation_id is null
            and chatwoot_message_id is null
            and failure_code is null
            and finalized_at is null)
        or
        (status = 'accepted_by_chatwoot'
            and chatwoot_conversation_id is not null
            and chatwoot_conversation_id > 0
            and chatwoot_message_id is not null
            and chatwoot_message_id > 0
            and failure_code is null
            and finalized_at is not null)
        or
        (status = 'delivery_unknown'
            and failure_code is not null
            and nullif(btrim(failure_code), '') is not null
            and finalized_at is not null)
    )
);

alter table public.johanna_abandonment_one_shot_commands enable row level security;

create or replace function public.protect_johanna_abandonment_one_shot_command()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000', message = 'johanna_abandonment_one_shot_command_immutable';
    end if;
    if old.id is distinct from new.id
       or old.command_key is distinct from new.command_key
       or old.semantic_fingerprint is distinct from new.semantic_fingerprint
       or old.rollout_scope is distinct from new.rollout_scope
       or old.purchase_intent_id is distinct from new.purchase_intent_id
       or old.scope_key is distinct from new.scope_key
       or old.scope_version is distinct from new.scope_version
       or old.runtime_generation is distinct from new.runtime_generation
       or old.chatwoot_account_id is distinct from new.chatwoot_account_id
       or old.chatwoot_inbox_id is distinct from new.chatwoot_inbox_id
       or old.target_phone is distinct from new.target_phone
       or old.template_name is distinct from new.template_name
       or old.template_language is distinct from new.template_language
       or old.template_category is distinct from new.template_category
       or old.copy_version is distinct from new.copy_version
       or old.max_messages is distinct from new.max_messages
       or old.followups_allowed is distinct from new.followups_allowed
       or old.created_at is distinct from new.created_at
       or old.status <> 'request_started'
       or new.status not in ('accepted_by_chatwoot', 'delivery_unknown') then
        raise exception using errcode = '55000', message = 'johanna_abandonment_one_shot_command_immutable';
    end if;
    return new;
end;
$function$;

create trigger johanna_abandonment_one_shot_commands_immutable
before update or delete on public.johanna_abandonment_one_shot_commands
for each row execute function public.protect_johanna_abandonment_one_shot_command();

create or replace function public.begin_johanna_abandonment_one_shot(
    p_command_key text,
    p_purchase_intent_id uuid,
    p_allowed_external_user_id text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_scope_key text,
    p_scope_version integer,
    p_expected_generation bigint
)
returns table (
    outcome text,
    command_id uuid,
    command_status text,
    target_phone text,
    buyer_name text,
    buyer_email text,
    product_name text,
    template_name text,
    template_language text,
    template_category text,
    copy_version text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    existing public.johanna_abandonment_one_shot_commands%rowtype;
    intent public.purchase_intents%rowtype;
    submission public.precheckout_submissions%rowtype;
    scope public.pilot_scope_versions%rowtype;
    control public.pilot_runtime_controls%rowtype;
    command_id_value uuid;
    fingerprint text;
    phone_owner_count integer;
    blocked_owner_count integer;
begin
    if p_command_key is null or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_purchase_intent_id is null
       or p_allowed_external_user_id is null
       or p_allowed_external_user_id !~ '^[1-9][0-9]{7,14}$'
       or p_chatwoot_account_id is distinct from 1
       or p_chatwoot_inbox_id is distinct from 9
       or p_scope_key is distinct from 'johanna-abandonment-template-e2e'
       or p_scope_version is distinct from 1
       or p_expected_generation is distinct from 0 then
        raise exception using errcode = '22023', message = 'johanna_abandonment_one_shot_input_invalid';
    end if;

    fingerprint := encode(sha256(convert_to(concat_ws(
        chr(31), p_purchase_intent_id::text, p_allowed_external_user_id,
        p_chatwoot_account_id::text, p_chatwoot_inbox_id::text,
        p_scope_key, p_scope_version::text, p_expected_generation::text
    ), 'UTF8')), 'hex');

    perform pg_advisory_xact_lock(hashtextextended('johanna-abandonment-template-e2e-v1', 0));

    select cmd.* into existing
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.command_key = p_command_key
    for update;

    if found then
        if existing.semantic_fingerprint is distinct from fingerprint then
            raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_command_conflict';
        end if;
        select ps.* into strict submission
        from public.purchase_intent_submissions link
        join public.precheckout_submissions ps on ps.id = link.submission_id
        where link.purchase_intent_id = existing.purchase_intent_id
        order by link.ordinal desc
        limit 1;
        return query select 'replay'::text, existing.id, existing.status,
            existing.target_phone,
            submission.canonical_payload #>> '{lead,full_name}',
            submission.canonical_payload #>> '{identity,email}',
            submission.canonical_payload #>> '{commerce,product_name}',
            existing.template_name, existing.template_language,
            existing.template_category, existing.copy_version;
        return;
    end if;

    if exists (
        select 1 from public.johanna_abandonment_one_shot_commands
        where rollout_scope = 'johanna-abandonment-template-e2e-v1'
    ) then
        raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_budget_consumed';
    end if;

    select published.* into strict scope
    from public.pilot_scope_versions published
    where published.scope_key = p_scope_key
      and published.version = p_scope_version
      and published.status = 'published'
      and published.tenant_key = 'lancemos'
      and published.channel_provider = 'waba'
      and published.channel_account_ref = 'chatwoot-inbox:9'
      and published.chatwoot_account_id = p_chatwoot_account_id
      and published.chatwoot_inbox_id = p_chatwoot_inbox_id
      and published.external_product_id = '8104005'
      and published.offer_code = 'bxjge6zq'
    for share;

    select runtime.* into strict control
    from public.pilot_runtime_controls runtime
    where runtime.scope_key = p_scope_key
      and runtime.scope_version = p_scope_version
    for update;

    if control.runtime_state <> 'inactive' then
        raise exception using errcode = '55000', message = 'johanna_abandonment_one_shot_runtime_not_inactive';
    end if;
    if control.generation is distinct from p_expected_generation then
        raise exception using errcode = '40001', message = 'johanna_abandonment_one_shot_generation_mismatch';
    end if;

    select candidate.* into strict intent
    from public.purchase_intents candidate
    where candidate.id = p_purchase_intent_id
    for update;

    if intent.tenant_ref <> 'lancemos'
       or intent.funnel_ref <> 'psicologajohanna'
       or intent.landing_ref <> 'ads-a'
       or intent.product_ref <> 'F106691755G'
       or intent.offer_ref <> 'bxjge6zq'
       or intent.lifecycle_state <> 'waiting_for_purchase'
       or intent.provisional
       or not intent.provider_observed
       or not intent.whatsapp_contact_authorized
       or not intent.activation_authorized
       or intent.current_classification is not null
       or intent.normalized_phone is distinct from p_allowed_external_user_id then
        raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_intent_not_authorized';
    end if;

    select ps.* into strict submission
    from public.purchase_intent_submissions link
    join public.precheckout_submissions ps on ps.id = link.submission_id
    where link.purchase_intent_id = intent.id
      and ps.contract_version = '1.1.0'
      and not ps.provisional
      and ps.provider_observed
      and ps.activation_authorized
      and ps.canonical_payload #>> '{consent,marketing_optin}' = 'true'
      and ps.canonical_payload #>> '{consent,whatsapp_contact}' = 'true'
      and ps.canonical_payload #>> '{consent,copy_version}' = 'johanna-precheckout-whatsapp-disclosure-v1'
      and ps.canonical_payload #>> '{identity,phone}' = intent.normalized_phone
      and ps.canonical_payload #>> '{commerce,offer_ref}' = 'bxjge6zq'
      and nullif(btrim(ps.canonical_payload #>> '{lead,full_name}'), '') is not null
      and nullif(btrim(ps.canonical_payload #>> '{commerce,product_name}'), '') is not null
      and not exists (
          select 1 from public.precheckout_submission_conflicts conflict
          where conflict.existing_submission_id = ps.id
            and conflict.resolved_at is null
      )
    order by link.ordinal desc
    limit 1;

    -- Serialize with the canonical inbound opt-out writer before request-start.
    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':',
            'chatwoot-opt-out-user',
            p_chatwoot_account_id,
            intent.normalized_phone
        ),
        0
    ));

    -- Follow the canonical authority order: contact first, then channel identity.
    perform 1
    from public.contacts owner
    where owner.id in (
        select point.contact_id
        from public.contact_points point
        where point.type = 'phone'
          and point.normalized_value = intent.normalized_phone
        union
        select identity.contact_id
        from public.channel_identities identity
        where identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
          and identity.external_user_id = intent.normalized_phone
          and identity.identity_status = 'active'
          and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
    )
    order by owner.id
    for update of owner;

    perform 1
    from public.channel_identities identity
    where identity.channel = 'whatsapp'
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.external_user_id = intent.normalized_phone
      and identity.identity_status = 'active'
      and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
    order by identity.id
    for update of identity;

    if exists (
        select 1
        from public.contact_opt_out_events stop
        where stop.channel = 'whatsapp'
          and stop.purpose = 'cart_recovery'
          and stop.source = 'chatwoot'
          and stop.canonical_account_id = p_chatwoot_account_id
          and stop.external_user_id = intent.normalized_phone
    ) then
        raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_contact_blocked';
    end if;

    select count(distinct point.contact_id)::integer,
           count(distinct point.contact_id) filter (
               where owner.contact_permission in ('opted_out', 'blocked', 'restricted')
                  or owner.lifecycle_status = 'do_not_contact'
           )::integer
    into phone_owner_count, blocked_owner_count
    from public.contact_points point
    join public.contacts owner on owner.id = point.contact_id
    where point.type = 'phone'
      and point.normalized_value = intent.normalized_phone;

    if phone_owner_count > 1 then
        raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_phone_ambiguous';
    end if;
    if blocked_owner_count > 0 then
        raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_contact_blocked';
    end if;

    insert into public.johanna_abandonment_one_shot_commands (
        command_key, semantic_fingerprint, rollout_scope, purchase_intent_id,
        scope_key, scope_version, runtime_generation,
        chatwoot_account_id, chatwoot_inbox_id, target_phone,
        template_name, template_language, template_category, copy_version,
        max_messages, followups_allowed, status
    ) values (
        p_command_key, fingerprint, 'johanna-abandonment-template-e2e-v1', intent.id,
        p_scope_key, p_scope_version, p_expected_generation,
        p_chatwoot_account_id, p_chatwoot_inbox_id, intent.normalized_phone,
        'johanna_carrito_abandonado_01', 'es_EC', 'MARKETING',
        'johanna-abandonment-one-shot-v1', 1, 0, 'request_started'
    ) returning id into command_id_value;

    return query select 'started'::text, command_id_value, 'request_started'::text,
        intent.normalized_phone,
        submission.canonical_payload #>> '{lead,full_name}',
        submission.canonical_payload #>> '{identity,email}',
        submission.canonical_payload #>> '{commerce,product_name}',
        'johanna_carrito_abandonado_01'::text, 'es_EC'::text,
        'MARKETING'::text, 'johanna-abandonment-one-shot-v1'::text;
end;
$function$;

create or replace function public.finish_johanna_abandonment_one_shot(
    p_command_id uuid,
    p_outcome text,
    p_chatwoot_conversation_id bigint,
    p_chatwoot_message_id bigint,
    p_failure_code text
)
returns table (command_id uuid, command_status text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    command public.johanna_abandonment_one_shot_commands%rowtype;
begin
    if p_command_id is null
       or p_outcome not in ('accepted_by_chatwoot', 'delivery_unknown')
       or (p_outcome = 'accepted_by_chatwoot' and (
           p_chatwoot_conversation_id is null or p_chatwoot_conversation_id < 1
           or p_chatwoot_message_id is null or p_chatwoot_message_id < 1
           or p_failure_code is not null
       ))
       or (p_outcome = 'delivery_unknown' and (
           p_failure_code is null or nullif(btrim(p_failure_code), '') is null
       )) then
        raise exception using errcode = '22023', message = 'johanna_abandonment_one_shot_finish_invalid';
    end if;

    select cmd.* into strict command
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.id = p_command_id
    for update;

    if command.status = p_outcome then
        return query select command.id, command.status;
        return;
    end if;
    if command.status <> 'request_started' then
        raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_finish_conflict';
    end if;

    update public.johanna_abandonment_one_shot_commands
    set status = p_outcome,
        chatwoot_conversation_id = p_chatwoot_conversation_id,
        chatwoot_message_id = p_chatwoot_message_id,
        failure_code = case when p_outcome = 'delivery_unknown' then btrim(p_failure_code) end,
        finalized_at = clock_timestamp()
    where id = command.id;

    return query select command.id, p_outcome;
end;
$function$;

revoke all on table public.johanna_abandonment_one_shot_commands from public;
revoke all on function public.protect_johanna_abandonment_one_shot_command() from public;
revoke all on function public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint) from public;
revoke all on function public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.johanna_abandonment_one_shot_commands from anon;
        revoke all on function public.protect_johanna_abandonment_one_shot_command() from anon;
        revoke all on function public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint) from anon;
        revoke all on function public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text) from anon;
    end if;

    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.johanna_abandonment_one_shot_commands from authenticated;
        revoke all on function public.protect_johanna_abandonment_one_shot_command() from authenticated;
        revoke all on function public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint) from authenticated;
        revoke all on function public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text) from authenticated;
    end if;

    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.johanna_abandonment_one_shot_commands from service_role;
        revoke all on function public.protect_johanna_abandonment_one_shot_command() from service_role;
        grant execute on function public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint) to service_role;
        grant execute on function public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text) to service_role;
    end if;
end;
$roles$;

commit;
