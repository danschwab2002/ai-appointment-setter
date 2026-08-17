-- Cut A: introduce a shadow commercial-case root without changing runtime
-- authority, handoff admission, scheduling, or outbound behavior.

begin;

create table public.commercial_cases (
    id uuid primary key,
    recovery_case_id uuid unique
        references public.recovery_cases(id) on delete cascade,
    case_kind text not null check (
        case_kind = any (array['inbound_sales', 'cart_recovery', 'payment_failure'])
    ),
    contact_id uuid not null references public.contacts(id) on delete restrict,
    selected_channel_identity_id uuid,
    conversation_id uuid,
    product_ref text,
    offer_ref text,
    status text not null check (
        status = any (array['active', 'paused', 'completed', 'cancelled', 'error'])
    ),
    automation_status text not null default 'draft_only' check (
        automation_status = any (
            array['draft_only', 'enabled', 'paused', 'disabled', 'restricted']
        )
    ),
    identity_resolution_status text check (
        identity_resolution_status is null
        or identity_resolution_status = any (
            array['resolved', 'candidate', 'ambiguous', 'conflict', 'unmatched']
        )
    ),
    authority_mode text not null default 'shadow' check (authority_mode = 'shadow'),
    version bigint not null default 1 check (version > 0),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    check (
        (case_kind = 'cart_recovery' and recovery_case_id = id)
        or (case_kind <> 'cart_recovery' and recovery_case_id is null)
    ),
    check (case_kind <> 'cart_recovery' or nullif(btrim(product_ref), '') is not null),
    check (conversation_id is null or selected_channel_identity_id is not null)
);

alter table public.commercial_cases enable row level security;

alter table public.recovery_cases
    add column commercial_case_id uuid;

insert into public.commercial_cases (
    id,
    recovery_case_id,
    case_kind,
    contact_id,
    selected_channel_identity_id,
    conversation_id,
    product_ref,
    offer_ref,
    status,
    automation_status,
    identity_resolution_status,
    authority_mode,
    version,
    created_at,
    updated_at
)
select rc.id,
       rc.id,
       'cart_recovery',
       rc.contact_id,
       rc.selected_channel_identity_id,
       rc.conversation_id,
       rc.external_product_id,
       rc.offer_code,
       case rc.status
           when 'grace_period' then 'active'
           when 'active' then 'active'
           when 'paused' then 'paused'
           when 'won' then 'completed'
           when 'sequence_exhausted' then 'completed'
           when 'lost' then 'completed'
           when 'cancelled' then 'cancelled'
           when 'unreachable' then 'completed'
           when 'expired' then 'completed'
           when 'escalated' then 'completed'
           when 'error' then 'error'
       end,
       case rc.status
           when 'grace_period' then 'enabled'
           when 'active' then 'enabled'
           when 'paused' then 'paused'
           when 'won' then 'disabled'
           when 'sequence_exhausted' then 'disabled'
           when 'lost' then 'disabled'
           when 'cancelled' then 'disabled'
           when 'unreachable' then 'disabled'
           when 'expired' then 'disabled'
           when 'escalated' then 'disabled'
           when 'error' then 'restricted'
       end,
       case rc.identity_resolution_status
           when 'resolved' then 'resolved'
           when 'ambiguous' then 'ambiguous'
           when 'not_found' then 'unmatched'
           else null
       end,
       'shadow',
       rc.version,
       rc.created_at,
       rc.updated_at
from public.recovery_cases rc;

update public.recovery_cases rc
set commercial_case_id = rc.id;

alter table public.recovery_cases
    alter column commercial_case_id set not null,
    add constraint recovery_cases_commercial_case_unique
        unique (commercial_case_id);

create function public.bind_recovery_commercial_case_id()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if new.commercial_case_id is not null
       and new.commercial_case_id <> new.id then
        raise exception using
            errcode = '23514',
            message = 'recovery_commercial_case_id_mismatch';
    end if;
    new.commercial_case_id := new.id;
    return new;
end;
$function$;

create trigger recovery_cases_bind_commercial_case_id
before insert or update on public.recovery_cases
for each row execute function public.bind_recovery_commercial_case_id();

create function public.sync_recovery_commercial_case()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'INSERT' then
        if exists (select 1 from public.commercial_cases cc where cc.id = new.id) then
            raise exception using
                errcode = '23505',
                message = 'commercial_case_id_already_exists';
        end if;

        insert into public.commercial_cases (
            id, recovery_case_id, case_kind, contact_id,
            selected_channel_identity_id, conversation_id,
            product_ref, offer_ref, status, automation_status,
            identity_resolution_status, authority_mode, version,
            created_at, updated_at
        ) values (
            new.id, new.id, 'cart_recovery', new.contact_id,
            new.selected_channel_identity_id, new.conversation_id,
            new.external_product_id, new.offer_code,
            case new.status
                when 'grace_period' then 'active'
                when 'active' then 'active'
                when 'paused' then 'paused'
                when 'won' then 'completed'
                when 'sequence_exhausted' then 'completed'
                when 'lost' then 'completed'
                when 'cancelled' then 'cancelled'
                when 'unreachable' then 'completed'
                when 'expired' then 'completed'
                when 'escalated' then 'completed'
                when 'error' then 'error'
            end,
            case new.status
                when 'grace_period' then 'enabled'
                when 'active' then 'enabled'
                when 'paused' then 'paused'
                when 'won' then 'disabled'
                when 'sequence_exhausted' then 'disabled'
                when 'lost' then 'disabled'
                when 'cancelled' then 'disabled'
                when 'unreachable' then 'disabled'
                when 'expired' then 'disabled'
                when 'escalated' then 'disabled'
                when 'error' then 'restricted'
            end,
            case new.identity_resolution_status
                when 'resolved' then 'resolved'
                when 'ambiguous' then 'ambiguous'
                when 'not_found' then 'unmatched'
                else null
            end,
            'shadow', new.version, new.created_at, new.updated_at
        );
    else
        update public.commercial_cases cc
        set contact_id = new.contact_id,
            selected_channel_identity_id = new.selected_channel_identity_id,
            conversation_id = new.conversation_id,
            product_ref = new.external_product_id,
            offer_ref = new.offer_code,
            status = case new.status
                when 'grace_period' then 'active'
                when 'active' then 'active'
                when 'paused' then 'paused'
                when 'won' then 'completed'
                when 'sequence_exhausted' then 'completed'
                when 'lost' then 'completed'
                when 'cancelled' then 'cancelled'
                when 'unreachable' then 'completed'
                when 'expired' then 'completed'
                when 'escalated' then 'completed'
                when 'error' then 'error'
            end,
            automation_status = case new.status
                when 'grace_period' then 'enabled'
                when 'active' then 'enabled'
                when 'paused' then 'paused'
                when 'won' then 'disabled'
                when 'sequence_exhausted' then 'disabled'
                when 'lost' then 'disabled'
                when 'cancelled' then 'disabled'
                when 'unreachable' then 'disabled'
                when 'expired' then 'disabled'
                when 'escalated' then 'disabled'
                when 'error' then 'restricted'
            end,
            identity_resolution_status = case new.identity_resolution_status
                when 'resolved' then 'resolved'
                when 'ambiguous' then 'ambiguous'
                when 'not_found' then 'unmatched'
                else null
            end,
            version = new.version,
            created_at = new.created_at,
            updated_at = new.updated_at
        where cc.id = new.id
          and cc.recovery_case_id = new.id
          and cc.case_kind = 'cart_recovery'
          and cc.authority_mode = 'shadow';

        if not found then
            raise exception using
                errcode = '23514',
                message = 'commercial_case_root_missing_or_not_shadow';
        end if;
    end if;

    return new;
end;
$function$;

create trigger recovery_cases_sync_commercial_case
after insert or update on public.recovery_cases
for each row execute function public.sync_recovery_commercial_case();

create function public.protect_commercial_case_shadow()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_recovery public.recovery_cases%rowtype;
begin
    if tg_op = 'DELETE' then
        if old.case_kind = 'cart_recovery'
           and exists (
               select 1 from public.recovery_cases rc
               where rc.id = old.recovery_case_id
           ) then
            raise exception using
                errcode = '23503',
                message = 'commercial_case_delete_requires_recovery_delete';
        end if;
        return old;
    end if;

    if new.case_kind <> 'cart_recovery' then
        raise exception using
            errcode = '55000',
            message = 'commercial_case_kind_not_enabled';
    end if;

    select rc.* into v_recovery
    from public.recovery_cases rc
    where rc.id = new.recovery_case_id
      and rc.id = new.id
      and rc.commercial_case_id = new.id;

    if not found then
        raise exception using
            errcode = '23514',
            message = 'commercial_case_root_missing_recovery';
    end if;

    if new.selected_channel_identity_id is not null
       and not exists (
           select 1
           from public.channel_identities ci
           where ci.id = new.selected_channel_identity_id
             and ci.contact_id = new.contact_id
       ) then
        raise exception using
            errcode = '23514',
            message = 'commercial_case_identity_contact_mismatch';
    end if;

    if new.conversation_id is not null
       and not exists (
           select 1
           from public.conversations c
           where c.id = new.conversation_id
             and c.contact_id = new.contact_id
             and c.channel_identity_id = new.selected_channel_identity_id
       ) then
        raise exception using
            errcode = '23514',
            message = 'commercial_case_conversation_mismatch';
    end if;

    if new.contact_id is distinct from v_recovery.contact_id
       or new.selected_channel_identity_id is distinct from v_recovery.selected_channel_identity_id
       or new.conversation_id is distinct from v_recovery.conversation_id
       or new.product_ref is distinct from v_recovery.external_product_id
       or new.offer_ref is distinct from v_recovery.offer_code
       or new.status is distinct from (case v_recovery.status
           when 'grace_period' then 'active'
           when 'active' then 'active'
           when 'paused' then 'paused'
           when 'won' then 'completed'
           when 'sequence_exhausted' then 'completed'
           when 'lost' then 'completed'
           when 'cancelled' then 'cancelled'
           when 'unreachable' then 'completed'
           when 'expired' then 'completed'
           when 'escalated' then 'completed'
           when 'error' then 'error'
       end)
       or new.automation_status is distinct from (case v_recovery.status
           when 'grace_period' then 'enabled'
           when 'active' then 'enabled'
           when 'paused' then 'paused'
           when 'won' then 'disabled'
           when 'sequence_exhausted' then 'disabled'
           when 'lost' then 'disabled'
           when 'cancelled' then 'disabled'
           when 'unreachable' then 'disabled'
           when 'expired' then 'disabled'
           when 'escalated' then 'disabled'
           when 'error' then 'restricted'
       end)
       or new.identity_resolution_status is distinct from (case v_recovery.identity_resolution_status
           when 'resolved' then 'resolved'
           when 'ambiguous' then 'ambiguous'
           when 'not_found' then 'unmatched'
           else null
       end)
       or new.version is distinct from v_recovery.version
       or new.created_at is distinct from v_recovery.created_at
       or new.updated_at is distinct from v_recovery.updated_at
       or new.authority_mode <> 'shadow' then
        raise exception using
            errcode = '23514',
            message = 'commercial_case_root_mismatch';
    end if;

    return new;
end;
$function$;

create trigger commercial_cases_protect_shadow
before insert or update or delete on public.commercial_cases
for each row execute function public.protect_commercial_case_shadow();

create function public.validate_recovery_commercial_case_shadow()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_recovery public.recovery_cases%rowtype;
    v_case public.commercial_cases%rowtype;
begin
    select rc.* into v_recovery
    from public.recovery_cases rc
    where rc.id = new.id;

    if not found then
        if exists (
            select 1 from public.commercial_cases cc
            where cc.id = new.id or cc.recovery_case_id = new.id
        ) then
            raise exception using
                errcode = '23514',
                message = 'commercial_case_orphan_after_recovery_delete';
        end if;
        return new;
    end if;

    select cc.* into v_case
    from public.commercial_cases cc
    where cc.id = v_recovery.commercial_case_id
      and cc.recovery_case_id = v_recovery.id;

    if not found then
        raise exception using
            errcode = '23514',
            message = 'recovery_commercial_case_root_missing';
    end if;

    return new;
end;
$function$;

create constraint trigger recovery_cases_validate_commercial_case_shadow
after insert or update on public.recovery_cases
deferrable initially deferred
for each row execute function public.validate_recovery_commercial_case_shadow();

revoke all on table public.commercial_cases from public;
revoke execute on function public.bind_recovery_commercial_case_id() from public;
revoke execute on function public.sync_recovery_commercial_case() from public;
revoke execute on function public.protect_commercial_case_shadow() from public;
revoke execute on function public.validate_recovery_commercial_case_shadow() from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.commercial_cases from anon;
        revoke execute on function public.bind_recovery_commercial_case_id() from anon;
        revoke execute on function public.sync_recovery_commercial_case() from anon;
        revoke execute on function public.protect_commercial_case_shadow() from anon;
        revoke execute on function public.validate_recovery_commercial_case_shadow() from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.commercial_cases from authenticated;
        revoke execute on function public.bind_recovery_commercial_case_id() from authenticated;
        revoke execute on function public.sync_recovery_commercial_case() from authenticated;
        revoke execute on function public.protect_commercial_case_shadow() from authenticated;
        revoke execute on function public.validate_recovery_commercial_case_shadow() from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.commercial_cases from service_role;
        revoke execute on function public.bind_recovery_commercial_case_id() from service_role;
        revoke execute on function public.sync_recovery_commercial_case() from service_role;
        revoke execute on function public.protect_commercial_case_shadow() from service_role;
        revoke execute on function public.validate_recovery_commercial_case_shadow() from service_role;
    end if;
end;
$roles$;

commit;
