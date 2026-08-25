-- Reconcile one observed Johanna WABA delivery without another provider effect.

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

    if current_setting('app.johanna_one_shot_reconciliation', true) = 'on'
       and old.status = 'delivery_unknown'
       and new.status = 'accepted_by_chatwoot'
       and old.id is not distinct from new.id
       and old.command_key is not distinct from new.command_key
       and old.semantic_fingerprint is not distinct from new.semantic_fingerprint
       and old.rollout_scope is not distinct from new.rollout_scope
       and old.purchase_intent_id is not distinct from new.purchase_intent_id
       and old.scope_key is not distinct from new.scope_key
       and old.scope_version is not distinct from new.scope_version
       and old.runtime_generation is not distinct from new.runtime_generation
       and old.chatwoot_account_id is not distinct from new.chatwoot_account_id
       and old.chatwoot_inbox_id is not distinct from new.chatwoot_inbox_id
       and old.target_phone is not distinct from new.target_phone
       and old.template_name is not distinct from new.template_name
       and old.template_language is not distinct from new.template_language
       and old.template_category is not distinct from new.template_category
       and old.copy_version is not distinct from new.copy_version
       and old.max_messages is not distinct from new.max_messages
       and old.followups_allowed is not distinct from new.followups_allowed
       and old.created_at is not distinct from new.created_at
       and new.chatwoot_conversation_id is not null
       and new.chatwoot_conversation_id > 0
       and new.chatwoot_message_id is not null
       and new.chatwoot_message_id > 0
       and new.failure_code is null
       and new.finalized_at is not null then
        return new;
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

create or replace function public.reconcile_johanna_abandonment_one_shot(
    p_command_key text,
    p_chatwoot_conversation_id bigint,
    p_chatwoot_message_id bigint
)
returns table (command_id uuid, command_status text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    command public.johanna_abandonment_one_shot_commands%rowtype;
begin
    if p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_chatwoot_conversation_id is null
       or p_chatwoot_conversation_id < 1
       or p_chatwoot_message_id is null
       or p_chatwoot_message_id < 1 then
        raise exception using errcode = '22023', message = 'johanna_abandonment_one_shot_reconcile_invalid';
    end if;

    select cmd.* into strict command
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.command_key = p_command_key
    for update;

    if command.status = 'accepted_by_chatwoot' then
        if command.chatwoot_conversation_id is distinct from p_chatwoot_conversation_id
           or command.chatwoot_message_id is distinct from p_chatwoot_message_id then
            raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_reconcile_conflict';
        end if;
        return query select command.id, command.status;
        return;
    end if;

    if command.status <> 'delivery_unknown' then
        raise exception using errcode = '23514', message = 'johanna_abandonment_one_shot_reconcile_conflict';
    end if;

    perform set_config('app.johanna_one_shot_reconciliation', 'on', true);

    update public.johanna_abandonment_one_shot_commands
    set status = 'accepted_by_chatwoot',
        chatwoot_conversation_id = p_chatwoot_conversation_id,
        chatwoot_message_id = p_chatwoot_message_id,
        failure_code = null,
        finalized_at = clock_timestamp()
    where id = command.id;

    return query select command.id, 'accepted_by_chatwoot'::text;
end;
$function$;

revoke all on function public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint) to service_role;
    end if;
end
$roles$;
