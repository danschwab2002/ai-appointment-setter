-- Close the direct PostgREST/RPC surface of the deferred trigger function.
-- Trigger execution does not require callers to hold EXECUTE on the function.

begin;

revoke all on function public.stop_cart_recovery_for_known_purchase()
from public;

do $privileges$
declare
    v_role text;
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select 1 from pg_roles where rolname = v_role) then
            execute format(
                'revoke all on function public.stop_cart_recovery_for_known_purchase() from %I',
                v_role
            );
        end if;
    end loop;
end;
$privileges$;

commit;
