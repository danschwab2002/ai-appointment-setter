-- Close the internal accepted-finalization bypass on Supabase projects whose
-- default privileges grant EXECUTE directly to API roles.

begin;

revoke execute on function public._finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) from public;

do $privileges$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke execute on function public._finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke execute on function public._finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke execute on function public._finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) from service_role';
    end if;
end;
$privileges$;

commit;
