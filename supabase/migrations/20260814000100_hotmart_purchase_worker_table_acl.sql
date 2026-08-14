begin;

-- SELECT ... FOR UPDATE on delivery attempts requires UPDATE privilege. Keep
-- that privilege inside the hardened RPC rather than reopening direct table DML
-- to the API role.
alter function public.apply_hotmart_purchase_approved(
    uuid, text, text, text, text, text, timestamptz
) security definer;

-- Repair the temporary production grant while remaining portable to plain
-- PostgreSQL test stacks that do not define Supabase API roles.
do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke insert, update, delete
        on table public.followup_delivery_attempts
        from service_role;
        grant execute on function public.apply_hotmart_purchase_approved(
            uuid, text, text, text, text, text, timestamptz
        ) to service_role;
    end if;
end;
$acl$;

commit;
