-- Expand Johanna cart recovery to derive each eligible recipient durably.

begin;

create function public.begin_johanna_abandonment_hotmart_auto_v2(
    p_command_key text,
    p_hotmart_webhook_event_id uuid,
    p_purchase_intent_id uuid,
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
    v_target_phone text;
begin
    if p_command_key is null
       or p_hotmart_webhook_event_id is null
       or p_purchase_intent_id is null then
        raise exception using errcode = '22023',
            message = 'johanna_abandonment_hotmart_auto_v2_input_invalid';
    end if;

    select intent.normalized_phone
    into v_target_phone
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id;

    if v_target_phone is null
       or v_target_phone !~ '^[1-9][0-9]{7,14}$' then
        raise exception using errcode = '23514',
            message = 'johanna_abandonment_hotmart_auto_v2_recipient_invalid';
    end if;

    return query
    select result.*
    from public.begin_johanna_abandonment_hotmart_auto(
        p_command_key,
        p_hotmart_webhook_event_id,
        p_purchase_intent_id,
        v_target_phone,
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        p_scope_key,
        p_scope_version,
        p_expected_generation
    ) result;
end;
$function$;

revoke all on function public.begin_johanna_abandonment_hotmart_auto_v2(text, uuid, uuid, bigint, bigint, text, integer, bigint) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.begin_johanna_abandonment_hotmart_auto_v2(text, uuid, uuid, bigint, bigint, text, integer, bigint) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.begin_johanna_abandonment_hotmart_auto_v2(text, uuid, uuid, bigint, bigint, text, integer, bigint) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.begin_johanna_abandonment_hotmart_auto_v2(text, uuid, uuid, bigint, bigint, text, integer, bigint) to service_role;
    end if;
end
$roles$;

commit;
