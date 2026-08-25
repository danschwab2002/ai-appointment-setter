-- Exhaustive read-only ACL inventory for every public function.
-- Any acl_status other than "ok" is a deployment blocker.

with
expected_service_role(signature) as (
    values
        ('public.activate_lancemos_pilot_scope_version(text, integer, bigint, text, text)'),
        ('public.admit_and_correlate_hotmart_cart_abandonment(text, jsonb, text, text)'),
        ('public.admit_and_correlate_hotmart_purchase_approved(text, jsonb, text, text)'),
        ('public.admit_precheckout_form_submission(text, jsonb, jsonb)'),
        ('public.admit_observed_lead_precheckout(text, jsonb, jsonb)'),
        ('public.begin_precheckout_test_first_touch(text, uuid, text, bigint, bigint)'),
        ('public.begin_johanna_abandonment_one_shot(text, uuid, text, bigint, bigint, text, integer, bigint)'),
        ('public.admit_johanna_payment_failure(text, jsonb, text, text)'),
        ('public.begin_johanna_payment_failure_hotmart_auto(text, uuid, bigint, bigint)'),
        ('public.prepare_johanna_payment_failure_invalid_contact_retry(text, uuid, bigint, bigint)'),
        ('public.admit_inbound_commercial_case(text, integer, bigint, text)'),
        ('public.admit_inbound_commercial_case_v2(text, integer, bigint, text)'),
        ('public.apply_chatwoot_inbound_opt_out(bigint, bigint, bigint, bigint, text, timestamp with time zone, text)'),
        ('public.apply_hotmart_purchase_approved(uuid, text, text, text, text, text, timestamp with time zone)'),
        ('public.bootstrap_proactive_lead_identity(text, uuid, text, integer, bigint, text, text)'),
        ('public.claim_chatwoot_opt_out_projections(text, timestamp with time zone, interval, integer)'),
        ('public.claim_due_followup_actions(text, timestamp with time zone, interval, integer)'),
        ('public.claim_human_handoff_projection_effects(text, integer, integer, timestamp with time zone)'),
        ('public.confirm_operator_correlation_resolution(text, text, text, uuid, text, uuid)'),
        ('public.correlate_hotmart_purchase_intent(uuid)'),
        ('public.evaluate_lancemos_pilot_scope(text, integer, text, bigint, bigint, text, text, text, text, text, text, uuid)'),
        ('public.finish_precheckout_test_first_touch(uuid, text, bigint, bigint, text)'),
        ('public.finish_johanna_abandonment_one_shot(uuid, text, bigint, bigint, text)'),
        ('public.reconcile_johanna_abandonment_one_shot(text, bigint, bigint)'),
        ('public.begin_johanna_abandonment_hotmart_auto(text, uuid, uuid, text, bigint, bigint, text, integer, bigint)'),
        ('public.begin_johanna_abandonment_hotmart_auto_v2(text, uuid, uuid, bigint, bigint, text, integer, bigint)'),
        ('public.finalize_chatwoot_opt_out_projection(uuid, text, bigint, boolean, text, integer, timestamp with time zone)'),
        ('public.finalize_followup_delivery_attempt(uuid, uuid, text, bigint, text, text, uuid, text, timestamp with time zone, timestamp with time zone, timestamp with time zone)'),
        ('public.finalize_human_handoff_projection_effect(uuid, text, bigint, text, text, timestamp with time zone, timestamp with time zone)'),
        ('public.get_followup_chatwoot_context(uuid, text, bigint, timestamp with time zone)'),
        ('public.get_followup_execution_context(uuid, text, bigint, timestamp with time zone)'),
        ('public.get_human_handoff_projection_status()'),
        ('public.get_lancemos_pilot_runtime_status(text, integer, text, text, text)'),
        ('public.get_operator_unresolved_correlation(text, text, uuid)'),
        ('public.has_chatwoot_opt_out_stop(bigint, bigint, bigint, text)'),
        ('public.list_due_hotmart_abandonment_reevaluations(timestamp with time zone, integer)'),
        ('public.list_operator_unresolved_correlations(text, text, integer, uuid)'),
        ('public.mark_lancemos_pilot_request_started(uuid, uuid, text, bigint, timestamp with time zone)'),
        ('public.plan_lancemos_pilot_cart_recovery(uuid, uuid, text, text, text, text, integer, timestamp with time zone, bigint, bigint, text, text, integer)'),
        ('public.prepare_operator_correlation_resolution(text, text, text, uuid, text, uuid, text, uuid)'),
        ('public.reconcile_chatwoot_opt_out_stop(bigint, bigint, bigint, text)'),
        ('public.reconcile_followup_delivery_attempt(uuid, uuid, bigint, text, text, uuid, timestamp with time zone, text, timestamp with time zone)'),
        ('public.record_and_finalize_followup_acceptance(uuid, uuid, text, bigint, text, text, text, timestamp with time zone)'),
        ('public.reevaluate_hotmart_abandonment_timer(uuid, timestamp with time zone)'),
        ('public.reevaluate_followup_action(uuid, text, bigint, timestamp with time zone, boolean, text, text, timestamp with time zone, text, boolean, boolean, boolean, boolean, boolean)'),
        ('public.reserve_followup_delivery_attempt(uuid, text, bigint, bigint, bigint, text, text, timestamp with time zone)'),
        ('public.request_inbound_human_handoff(uuid, text, text, text, integer, timestamp with time zone)'),
        ('public.request_human_handoff(uuid, text, text, text, text, integer, uuid, uuid, text, bigint, timestamp with time zone)'),
        ('public.set_lancemos_pilot_cohort_member(text, integer, uuid, bigint, text, text, text)'),
        ('public.set_lancemos_pilot_runtime_state(text, integer, bigint, text, text, text)')
),
functions as (
    select
        p.oid,
        format(
            'public.%I(%s)',
            p.proname,
            oidvectortypes(p.proargtypes)
        ) as signature,
        p.prorettype::regtype::text as result_type,
        p.prosecdef as security_definer,
        coalesce(array_to_string(p.proconfig, ','), '') as function_config,
        has_function_privilege('anon', p.oid, 'execute') as anon_execute,
        has_function_privilege('authenticated', p.oid, 'execute') as authenticated_execute,
        has_function_privilege('service_role', p.oid, 'execute') as service_role_execute
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
)
select
    f.signature,
    f.result_type,
    f.security_definer,
    f.function_config,
    (e.signature is not null) as expected_service_role_execute,
    f.anon_execute,
    f.authenticated_execute,
    f.service_role_execute,
    case
        when f.anon_execute or f.authenticated_execute then 'api_role_execute_leak'
        when f.result_type = 'trigger' and f.service_role_execute then 'trigger_service_execute_leak'
        when f.service_role_execute is distinct from (e.signature is not null) then 'service_role_allowlist_mismatch'
        when f.security_definer and position('search_path=' in f.function_config) = 0 then 'security_definer_search_path_missing'
        else 'ok'
    end as acl_status
from functions f
left join expected_service_role e using (signature)
order by f.signature;
