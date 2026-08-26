-- Block inbound admission replay after durable pause or human takeover.

begin;

alter function public.admit_inbound_commercial_case(text, integer, bigint, text)
    rename to admit_inbound_commercial_case_base;

revoke all on function public.admit_inbound_commercial_case_base(
    text, integer, bigint, text
) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.admit_inbound_commercial_case_base(
            text, integer, bigint, text
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.admit_inbound_commercial_case_base(
            text, integer, bigint, text
        ) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on function public.admit_inbound_commercial_case_base(
            text, integer, bigint, text
        ) from service_role;
    end if;
end
$roles$;

create function public.admit_inbound_commercial_case_v2(
    p_scope_key text,
    p_scope_version integer,
    p_external_conversation_id bigint,
    p_external_user_id text
)
returns table (
    outcome text,
    commercial_case_id uuid,
    contact_id uuid,
    channel_identity_id uuid,
    conversation_id uuid,
    automation_status text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_base record;
    v_case public.commercial_cases%rowtype;
    v_conversation public.conversations%rowtype;
begin
    select result.*
    into strict v_base
    from public.admit_inbound_commercial_case_base(
        p_scope_key,
        p_scope_version,
        p_external_conversation_id,
        p_external_user_id
    ) result;

    if v_base.outcome = 'evidence_conflict' then
        outcome := v_base.outcome;
        commercial_case_id := v_base.commercial_case_id;
        contact_id := v_base.contact_id;
        channel_identity_id := v_base.channel_identity_id;
        conversation_id := v_base.conversation_id;
        automation_status := 'draft_only';
        return next;
        return;
    end if;

    select commercial_case.*
    into v_case
    from public.commercial_cases commercial_case
    where commercial_case.id = v_base.commercial_case_id
      and commercial_case.case_kind = 'inbound_sales'
      and commercial_case.contact_id = v_base.contact_id
      and commercial_case.selected_channel_identity_id = v_base.channel_identity_id
      and commercial_case.conversation_id = v_base.conversation_id
      and commercial_case.inbound_scope_key = p_scope_key
      and commercial_case.inbound_scope_version = p_scope_version
    for update;
    if not found then
        raise exception using errcode = 'P0001',
            message = 'inbound_commercial_case_replay_aggregate_missing';
    end if;

    select conversation.*
    into v_conversation
    from public.conversations conversation
    where conversation.id = v_base.conversation_id
      and conversation.contact_id = v_base.contact_id
      and conversation.channel_identity_id = v_base.channel_identity_id
      and conversation.commercial_context = jsonb_build_object(
          'chatwoot_conversation_id', p_external_conversation_id::text
      )
    for update;
    if not found then
        raise exception using errcode = 'P0001',
            message = 'inbound_commercial_case_replay_conversation_missing';
    end if;

    commercial_case_id := v_base.commercial_case_id;
    contact_id := v_base.contact_id;
    channel_identity_id := v_base.channel_identity_id;
    conversation_id := v_base.conversation_id;

    if v_case.status = 'active'
       and v_case.automation_status = 'draft_only'
       and v_conversation.status in (
           'active', 'awaiting_agent', 'awaiting_contact', 'snoozed'
       )
       and v_conversation.automation_status = 'draft_only'
       and not v_conversation.human_takeover then
        outcome := v_base.outcome;
        automation_status := 'draft_only';
    else
        outcome := 'blocked';
        automation_status := 'disabled';
    end if;

    return next;
end;
$function$;

create function public.admit_inbound_commercial_case(
    p_scope_key text,
    p_scope_version integer,
    p_external_conversation_id bigint,
    p_external_user_id text
)
returns table (
    outcome text,
    commercial_case_id uuid,
    contact_id uuid,
    channel_identity_id uuid,
    conversation_id uuid,
    automation_status text
)
language sql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select
        case when result.outcome = 'blocked'
            then 'evidence_conflict'
            else result.outcome
        end as outcome,
        result.commercial_case_id,
        result.contact_id,
        result.channel_identity_id,
        result.conversation_id,
        'draft_only'::text as automation_status
    from public.admit_inbound_commercial_case_v2(
        p_scope_key,
        p_scope_version,
        p_external_conversation_id,
        p_external_user_id
    ) result;
$function$;

revoke all on function public.admit_inbound_commercial_case_v2(
    text, integer, bigint, text
) from public;
revoke all on function public.admit_inbound_commercial_case(
    text, integer, bigint, text
) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.admit_inbound_commercial_case_v2(
            text, integer, bigint, text
        ) from anon;
        revoke all on function public.admit_inbound_commercial_case(
            text, integer, bigint, text
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.admit_inbound_commercial_case_v2(
            text, integer, bigint, text
        ) from authenticated;
        revoke all on function public.admit_inbound_commercial_case(
            text, integer, bigint, text
        ) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.admit_inbound_commercial_case_v2(
            text, integer, bigint, text
        ) to service_role;
        grant execute on function public.admit_inbound_commercial_case(
            text, integer, bigint, text
        ) to service_role;
    end if;
end
$roles$;

commit;
