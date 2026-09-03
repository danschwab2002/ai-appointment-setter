begin;

revoke all on function public.read_johanna_funnel_dashboard_v1(
    timestamptz, integer
) from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.read_johanna_funnel_dashboard_v1(
            timestamptz, integer
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.read_johanna_funnel_dashboard_v1(
            timestamptz, integer
        ) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on function public.read_johanna_funnel_dashboard_v1(
            timestamptz, integer
        ) from service_role;
    end if;
end;
$acl$;

commit;
