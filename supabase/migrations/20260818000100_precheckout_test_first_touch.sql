-- One-shot, operator-triggered first touch for the allowlisted pre-checkout E2E.
-- This does not classify abandonment, enable a scheduler, or authorize follow-ups.

begin;

create table public.precheckout_test_first_touch_commands (
    id uuid primary key default gen_random_uuid(),
    rollout_scope text not null default 'joana-libre-de-ansiedad-precheckout-test-v1' unique,
    command_key text not null unique,
    purchase_intent_id uuid not null unique references public.purchase_intents(id) on delete restrict,
    contact_id uuid not null references public.contacts(id) on delete restrict,
    channel_identity_id uuid not null references public.channel_identities(id) on delete restrict,
    conversation_id uuid references public.conversations(id) on delete restrict,
    template_name text not null,
    template_language text not null,
    template_category text not null,
    copy_version text not null,
    status text not null,
    test_only boolean not null default true,
    generalizable boolean not null default false,
    max_messages integer not null default 1,
    followups_allowed integer not null default 0,
    chatwoot_conversation_id bigint,
    chatwoot_message_id bigint,
    failure_code text,
    request_started_at timestamptz not null default now(),
    finalized_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (btrim(command_key) <> ''),
    check (rollout_scope = 'joana-libre-de-ansiedad-precheckout-test-v1'),
    check (template_name = 'libre_ansiedad_test_first_touch_v1'),
    check (template_language = 'es_AR'),
    check (template_category = 'MARKETING'),
    check (copy_version = 'libre-ansiedad-precheckout-first-touch-v1'),
    check (status in ('request_started', 'accepted_by_chatwoot', 'failed', 'delivery_unknown', 'cancelled')),
    check (test_only and not generalizable),
    check (max_messages = 1 and followups_allowed = 0),
    check (chatwoot_conversation_id is null or chatwoot_conversation_id > 0),
    check (chatwoot_message_id is null or chatwoot_message_id > 0),
    check (
        (status = 'request_started' and finalized_at is null and failure_code is null)
        or (status = 'accepted_by_chatwoot' and finalized_at is not null
            and chatwoot_conversation_id is not null and chatwoot_message_id is not null
            and failure_code is null)
        or (status in ('failed', 'delivery_unknown', 'cancelled') and finalized_at is not null
            and failure_code is not null)
    )
);

create or replace function public.begin_precheckout_test_first_touch(
    p_command_key text,
    p_purchase_intent_id uuid,
    p_allowed_external_user_id text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint
)
returns table (
    outcome text,
    command_id uuid,
    command_status text,
    target_phone text,
    buyer_name text,
    chatwoot_conversation_id bigint,
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
    v_existing public.precheckout_test_first_touch_commands%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_contact_id uuid;
    v_identity_id uuid;
    v_conversation_id uuid;
    v_external_conversation_id text;
    v_buyer_name text;
    v_command_id uuid;
    v_identity_count integer;
    v_conversation_count integer;
begin
    if p_command_key is null or btrim(p_command_key) = ''
       or p_purchase_intent_id is null
       or p_allowed_external_user_id is null
       or p_allowed_external_user_id !~ '^[1-9][0-9]{7,14}$'
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1 then
        raise exception using errcode = '22023', message = 'invalid_precheckout_first_touch_input';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_purchase_intent_id::text, 0));

    select cmd.* into v_existing
    from public.precheckout_test_first_touch_commands cmd
    where cmd.purchase_intent_id = p_purchase_intent_id
    for update;

    if found then
        if v_existing.command_key is distinct from p_command_key then
            raise exception using errcode = '23505', message = 'precheckout_first_touch_already_exists';
        end if;
        select pi.normalized_phone,
               ps.canonical_payload #>> '{lead,full_name}',
               ci.external_conversation_id
        into v_intent.normalized_phone, v_buyer_name, v_external_conversation_id
        from public.purchase_intents pi
        join public.purchase_intent_submissions pis on pis.purchase_intent_id = pi.id
        join public.precheckout_submissions ps on ps.id = pis.submission_id
        join public.channel_identities ci on ci.id = v_existing.channel_identity_id
        where pi.id = p_purchase_intent_id
        order by pis.ordinal desc
        limit 1;
        return query select
            'replay'::text,
            v_existing.id,
            v_existing.status,
            v_intent.normalized_phone,
            v_buyer_name,
            v_external_conversation_id::bigint,
            v_existing.template_name,
            v_existing.template_language,
            v_existing.template_category,
            v_existing.copy_version;
        return;
    end if;

    if exists (
        select 1
        from public.precheckout_test_first_touch_commands cmd
        where cmd.rollout_scope = 'joana-libre-de-ansiedad-precheckout-test-v1'
    ) then
        raise exception using errcode = '23505', message = 'precheckout_first_touch_rollout_consumed';
    end if;

    select pi.* into v_intent
    from public.purchase_intents pi
    where pi.id = p_purchase_intent_id
    for update;

    if not found then
        raise exception using errcode = 'P0002', message = 'precheckout_purchase_intent_not_found';
    end if;
    if v_intent.tenant_ref <> 'joana'
       or v_intent.funnel_ref <> 'libre-de-ansiedad'
       or v_intent.product_ref <> 'F106691755G'
       or v_intent.offer_ref <> 'bxjge6zq'
       or v_intent.lifecycle_state <> 'waiting_for_purchase'
       or not v_intent.provisional
       or v_intent.provider_observed
       or v_intent.activation_authorized
       or v_intent.whatsapp_contact_authorized then
        raise exception using errcode = '55000', message = 'precheckout_first_touch_intent_not_test_eligible';
    end if;
    if v_intent.normalized_phone is distinct from p_allowed_external_user_id then
        raise exception using errcode = '42501', message = 'precheckout_first_touch_target_not_allowed';
    end if;

    select count(*)
    into v_identity_count
    from public.channel_identities ci
    join public.contacts c on c.id = ci.contact_id
    where ci.channel = 'whatsapp'
      and ci.external_user_id = p_allowed_external_user_id
      and ci.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and ci.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and ci.identity_status = 'active'
      and ci.external_conversation_id ~ '^[1-9][0-9]*$'
      and c.contact_permission not in ('opted_out', 'blocked', 'restricted')
      and c.lifecycle_status <> 'do_not_contact';

    if v_identity_count <> 1 then
        raise exception using errcode = '55000', message = 'precheckout_first_touch_identity_not_unique';
    end if;

    select ci.id, ci.contact_id, ci.external_conversation_id
    into v_identity_id, v_contact_id, v_external_conversation_id
    from public.channel_identities ci
    join public.contacts c on c.id = ci.contact_id
    where ci.channel = 'whatsapp'
      and ci.external_user_id = p_allowed_external_user_id
      and ci.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and ci.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and ci.identity_status = 'active'
      and ci.external_conversation_id ~ '^[1-9][0-9]*$'
      and c.contact_permission not in ('opted_out', 'blocked', 'restricted')
      and c.lifecycle_status <> 'do_not_contact';

    if v_identity_id is null or v_contact_id is null then
        raise exception using errcode = '55000', message = 'precheckout_first_touch_identity_changed';
    end if;

    perform 1
    from public.contacts c
    where c.id = v_contact_id
      and c.contact_permission not in ('opted_out', 'blocked', 'restricted')
      and c.lifecycle_status <> 'do_not_contact'
    for update;
    if not found then
        raise exception using errcode = '55000', message = 'precheckout_first_touch_contact_changed';
    end if;

    perform 1
    from public.channel_identities ci
    where ci.id = v_identity_id
      and ci.contact_id = v_contact_id
      and ci.channel = 'whatsapp'
      and ci.external_user_id = p_allowed_external_user_id
      and ci.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and ci.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and ci.identity_status = 'active'
      and ci.external_conversation_id = v_external_conversation_id
    for update;
    if not found then
        raise exception using errcode = '55000', message = 'precheckout_first_touch_identity_changed';
    end if;

    select count(*)
    into v_conversation_count
    from public.conversations conv
    where conv.channel_identity_id = v_identity_id
      and conv.contact_id = v_contact_id
      and conv.status not in ('paused_human', 'closed', 'blocked')
      and conv.automation_status not in ('paused', 'disabled', 'restricted', 'error')
      and not conv.human_takeover
      and conv.commercial_context = jsonb_build_object(
          'chatwoot_conversation_id', v_external_conversation_id
      );

    if v_conversation_count <> 1 then
        raise exception using errcode = '55000', message = 'precheckout_first_touch_conversation_not_unique';
    end if;

    select conv.id into v_conversation_id
    from public.conversations conv
    where conv.channel_identity_id = v_identity_id
      and conv.contact_id = v_contact_id
      and conv.status not in ('paused_human', 'closed', 'blocked')
      and conv.automation_status not in ('paused', 'disabled', 'restricted', 'error')
      and not conv.human_takeover
      and conv.commercial_context = jsonb_build_object(
          'chatwoot_conversation_id', v_external_conversation_id
      )
    for update of conv;

    if v_conversation_id is null then
        raise exception using errcode = '55000', message = 'precheckout_first_touch_conversation_changed';
    end if;

    select ps.canonical_payload #>> '{lead,full_name}' into v_buyer_name
    from public.purchase_intent_submissions pis
    join public.precheckout_submissions ps on ps.id = pis.submission_id
    where pis.purchase_intent_id = p_purchase_intent_id
    order by pis.ordinal desc
    limit 1;

    if v_buyer_name is null or btrim(v_buyer_name) = '' then
        raise exception using errcode = '55000', message = 'precheckout_first_touch_buyer_name_missing';
    end if;

    insert into public.precheckout_test_first_touch_commands (
        command_key,
        purchase_intent_id,
        contact_id,
        channel_identity_id,
        conversation_id,
        template_name,
        template_language,
        template_category,
        copy_version,
        status
    ) values (
        p_command_key,
        p_purchase_intent_id,
        v_contact_id,
        v_identity_id,
        v_conversation_id,
        'libre_ansiedad_test_first_touch_v1',
        'es_AR',
        'MARKETING',
        'libre-ansiedad-precheckout-first-touch-v1',
        'request_started'
    ) returning id into v_command_id;

    return query select
        'started'::text,
        v_command_id,
        'request_started'::text,
        v_intent.normalized_phone,
        v_buyer_name,
        v_external_conversation_id::bigint,
        'libre_ansiedad_test_first_touch_v1'::text,
        'es_AR'::text,
        'MARKETING'::text,
        'libre-ansiedad-precheckout-first-touch-v1'::text;
end;
$function$;

create or replace function public.finish_precheckout_test_first_touch(
    p_command_id uuid,
    p_outcome text,
    p_chatwoot_conversation_id bigint default null,
    p_chatwoot_message_id bigint default null,
    p_failure_code text default null
)
returns table (
    command_id uuid,
    command_status text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_command public.precheckout_test_first_touch_commands%rowtype;
begin
    if p_command_id is null or p_outcome not in ('accepted_by_chatwoot', 'failed', 'delivery_unknown') then
        raise exception using errcode = '22023', message = 'invalid_precheckout_first_touch_finish_input';
    end if;

    select cmd.* into v_command
    from public.precheckout_test_first_touch_commands cmd
    where cmd.id = p_command_id
    for update;

    if not found then
        raise exception using errcode = 'P0002', message = 'precheckout_first_touch_command_not_found';
    end if;
    if v_command.status <> 'request_started' then
        return query select v_command.id, v_command.status;
        return;
    end if;

    if p_outcome = 'accepted_by_chatwoot' then
        if p_chatwoot_conversation_id is null or p_chatwoot_conversation_id < 1
           or p_chatwoot_message_id is null or p_chatwoot_message_id < 1
           or p_failure_code is not null then
            raise exception using errcode = '22023', message = 'invalid_precheckout_first_touch_acceptance';
        end if;
        update public.precheckout_test_first_touch_commands
        set status = 'accepted_by_chatwoot',
            chatwoot_conversation_id = p_chatwoot_conversation_id,
            chatwoot_message_id = p_chatwoot_message_id,
            finalized_at = now(),
            updated_at = now()
        where id = p_command_id;
    else
        if p_failure_code is null or btrim(p_failure_code) = ''
           or p_chatwoot_conversation_id is not null
           or p_chatwoot_message_id is not null then
            raise exception using errcode = '22023', message = 'invalid_precheckout_first_touch_failure';
        end if;
        update public.precheckout_test_first_touch_commands
        set status = p_outcome,
            failure_code = left(p_failure_code, 120),
            finalized_at = now(),
            updated_at = now()
        where id = p_command_id;
    end if;

    return query
    select cmd.id, cmd.status
    from public.precheckout_test_first_touch_commands cmd
    where cmd.id = p_command_id;
end;
$function$;

revoke all on table public.precheckout_test_first_touch_commands from public;
revoke all on function public.begin_precheckout_test_first_touch(text, uuid, text, bigint, bigint) from public;
revoke all on function public.finish_precheckout_test_first_touch(uuid, text, bigint, bigint, text) from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.precheckout_test_first_touch_commands from anon;
        revoke all on function public.begin_precheckout_test_first_touch(text, uuid, text, bigint, bigint) from anon;
        revoke all on function public.finish_precheckout_test_first_touch(uuid, text, bigint, bigint, text) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.precheckout_test_first_touch_commands from authenticated;
        revoke all on function public.begin_precheckout_test_first_touch(text, uuid, text, bigint, bigint) from authenticated;
        revoke all on function public.finish_precheckout_test_first_touch(uuid, text, bigint, bigint, text) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.precheckout_test_first_touch_commands from service_role;
        grant execute on function public.begin_precheckout_test_first_touch(text, uuid, text, bigint, bigint) to service_role;
        grant execute on function public.finish_precheckout_test_first_touch(uuid, text, bigint, bigint, text) to service_role;
    end if;
end;
$acl$;

commit;
