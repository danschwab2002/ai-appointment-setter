-- Contract phase for Hotmart purchase-intent correlation.
-- All production replicas use the correlated wrappers; remove service-role
-- access to the temporary expand shims while retaining owner-only definitions
-- for catalog continuity and controlled rollback analysis.

begin;

do $contract_legacy_hotmart_shims$
begin
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on function public.admit_hotmart_purchase_approved(text, jsonb)
            from service_role;
        revoke all on function public.admit_hotmart_cart_abandonment(text, jsonb)
            from service_role;
    end if;
end;
$contract_legacy_hotmart_shims$;

commit;
