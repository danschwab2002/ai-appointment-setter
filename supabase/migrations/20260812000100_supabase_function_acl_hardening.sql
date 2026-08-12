begin;

-- Supabase may grant EXECUTE directly to API roles through default privileges.
-- Reset the complete public function surface, then restore only bridge RPCs.
do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    for v_function in
        select p.oid::regprocedure
        from pg_proc p
        join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public'
    loop
        execute format('revoke execute on function %s from public', v_function);
        for v_role in
            select r.rolname
            from pg_roles r
            where r.rolname in ('anon', 'authenticated', 'service_role')
        loop
            execute format(
                'revoke execute on function %s from %I',
                v_function,
                v_role
            );
        end loop;
    end loop;

    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.activate_lancemos_pilot_scope_version(text, integer, bigint, text, text) to service_role;
        grant execute on function public.admit_hotmart_cart_abandonment(text, jsonb) to service_role;
        grant execute on function public.admit_hotmart_purchase_approved(text, jsonb) to service_role;
        grant execute on function public.apply_chatwoot_inbound_opt_out(bigint, bigint, bigint, bigint, text, timestamptz, text) to service_role;
        grant execute on function public.apply_hotmart_purchase_approved(uuid, text, text, text, text, text, timestamptz) to service_role;
        grant execute on function public.claim_chatwoot_opt_out_projections(text, timestamptz, interval, integer) to service_role;
        grant execute on function public.claim_due_followup_actions(text, timestamptz, interval, integer) to service_role;
        grant execute on function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz) to service_role;
        grant execute on function public.evaluate_lancemos_pilot_scope(text, integer, text, bigint, bigint, text, text, text, text, text, text, uuid) to service_role;
        grant execute on function public.finalize_chatwoot_opt_out_projection(uuid, text, bigint, boolean, text, integer, timestamptz) to service_role;
        grant execute on function public.finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, timestamptz, timestamptz) to service_role;
        grant execute on function public.finalize_human_handoff_projection_effect(uuid, text, bigint, text, text, timestamptz, timestamptz) to service_role;
        grant execute on function public.get_followup_chatwoot_context(uuid, text, bigint, timestamptz) to service_role;
        grant execute on function public.get_followup_execution_context(uuid, text, bigint, timestamptz) to service_role;
        grant execute on function public.get_human_handoff_projection_status() to service_role;
        grant execute on function public.get_lancemos_pilot_runtime_status(text, integer, text, text, text) to service_role;
        grant execute on function public.has_chatwoot_opt_out_stop(bigint, bigint, bigint, text) to service_role;
        grant execute on function public.mark_lancemos_pilot_request_started(uuid, uuid, text, bigint, timestamptz) to service_role;
        grant execute on function public.plan_lancemos_pilot_cart_recovery(uuid, uuid, text, text, text, text, integer, timestamptz, bigint, bigint, text, text, integer) to service_role;
        grant execute on function public.reconcile_chatwoot_opt_out_stop(bigint, bigint, bigint, text) to service_role;
        grant execute on function public.reconcile_followup_delivery_attempt(uuid, uuid, bigint, text, text, uuid, timestamptz, text, timestamptz) to service_role;
        grant execute on function public.record_and_finalize_followup_acceptance(uuid, uuid, text, bigint, text, text, text, timestamptz) to service_role;
        grant execute on function public.reevaluate_followup_action(uuid, text, bigint, timestamptz, boolean, text, text, timestamptz, text, boolean, boolean, boolean, boolean, boolean) to service_role;
        grant execute on function public.request_human_handoff(uuid, text, text, text, text, integer, uuid, uuid, text, bigint, timestamptz) to service_role;
        grant execute on function public.reserve_followup_delivery_attempt(uuid, text, bigint, bigint, bigint, text, text, timestamptz) to service_role;
        grant execute on function public.set_lancemos_pilot_cohort_member(text, integer, uuid, bigint, text, text, text) to service_role;
        grant execute on function public.set_lancemos_pilot_runtime_state(text, integer, bigint, text, text, text) to service_role;
    end if;
end;
$acl$;

commit;
