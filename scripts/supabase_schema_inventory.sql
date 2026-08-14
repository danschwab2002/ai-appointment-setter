-- Read-only Supabase schema fingerprint for the canonical migration stack.
-- This query inspects catalogs only. It does not prove that a migration was run,
-- nor does it read application rows or migration payloads.

with
functions as (
    select
        p.oid,
        p.proname,
        pg_get_functiondef(p.oid) as definition
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
),
triggers as (
    select t.tgname
    from pg_trigger t
    join pg_class c on c.oid = t.tgrelid
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and not t.tgisinternal
      and t.tgenabled <> 'D'
),
indexes as (
    select indexname
    from pg_indexes
    where schemaname = 'public'
),
fingerprints(version, filename, present_markers, total_markers, classification) as (
    select
        '20260803000100',
        '20260803000100_followup_engine_v1.sql',
        (to_regclass('public.followup_policy_versions') is not null)::int
        + (to_regclass('public.contact_authorizations') is not null)::int
        + (to_regclass('public.recovery_case_events') is not null)::int
        + (to_regclass('public.followup_delivery_attempts') is not null)::int
        + exists(select 1 from functions where proname = 'claim_due_followup_actions')::int
        + exists(select 1 from triggers where tgname = 'scheduled_actions_protect_identity')::int
        + exists(select 1 from indexes where indexname = 'followup_delivery_attempts_in_flight_idx')::int,
        7,
        'catalog_objects'
    union all
    select
        '20260804000100',
        '20260804000100_followup_engine_permissions_hotfix.sql',
        coalesce((
            select (
                not has_function_privilege('anon', oid, 'execute')
                and not has_function_privilege('authenticated', oid, 'execute')
                and not has_function_privilege('service_role', oid, 'execute')
            )::int
            from functions
            where proname = '_finalize_followup_delivery_attempt'
        ), 0),
        1,
        'effective_acl'
    union all
    select
        '20260804000200',
        '20260804000200_followup_identity_binding.sql',
        exists(select 1 from functions where proname = 'plan_cart_recovery_with_identity')::int,
        1,
        'descendant_function_present'
    union all
    select
        '20260805000100',
        '20260805000100_followup_identity_audit.sql',
        exists(select 1 from functions where proname = 'record_resolved_identity_attempt')::int
        + exists(select 1 from triggers where tgname = 'recovery_cases_record_resolved_identity_attempt')::int,
        2,
        'catalog_objects'
    union all
    select
        '20260805000200',
        '20260805000200_followup_contact_authorization_grant.sql',
        exists(
            select 1 from functions
            where proname = 'plan_cart_recovery_with_identity'
              and position('contact_authorizations' in definition) > 0
              and position('hotmart' in definition) > 0
        )::int,
        1,
        'function_body_marker'
    union all
    select
        '20260805000300',
        '20260805000300_per_case_conversation_anchor.sql',
        exists(
            select 1 from functions
            where proname = 'get_followup_chatwoot_context'
              and position('case_conv' in definition) > 0
              and position('chatwoot_conversation_id' in definition) > 0
        )::int
        + exists(
            select 1 from functions
            where proname = 'record_and_finalize_followup_acceptance'
              and position('case_conversation_mismatch' in definition) > 0
        )::int,
        2,
        'function_body_markers'
    union all
    select
        '20260808000100',
        '20260808000100_hotmart_purchase_approved.sql',
        exists(select 1 from functions where proname = 'apply_hotmart_purchase_approved')::int
        + exists(select 1 from indexes where indexname = 'webhook_events_hotmart_purchase_transaction_unique_idx')::int,
        2,
        'catalog_objects'
    union all
    select
        '20260808000200',
        '20260808000200_hotmart_purchase_ordering_guard.sql',
        exists(select 1 from functions where proname = 'stop_cart_recovery_for_known_purchase')::int
        + exists(select 1 from triggers where tgname = 'scheduled_actions_stop_for_known_purchase')::int,
        2,
        'catalog_objects'
    union all
    select
        '20260808000300',
        '20260808000300_hotmart_purchase_ordering_guard_privileges.sql',
        coalesce((
            select (
                not has_function_privilege('anon', oid, 'execute')
                and not has_function_privilege('authenticated', oid, 'execute')
                and not has_function_privilege('service_role', oid, 'execute')
            )::int
            from functions
            where proname = 'stop_cart_recovery_for_known_purchase'
        ), 0),
        1,
        'effective_acl'
    union all
    select
        '20260808000400',
        '20260808000400_hotmart_purchase_safety_fences.sql',
        exists(select 1 from functions where proname = 'finalize_purchase_stopped_delivery_attempts')::int
        + exists(select 1 from functions where proname = 'fail_closed_ambiguous_known_purchase')::int
        + exists(select 1 from triggers where tgname = 'scheduled_actions_finalize_purchase_attempts')::int
        + exists(select 1 from triggers where tgname = 'scheduled_actions_fail_closed_known_purchase_ambiguity')::int,
        4,
        'catalog_objects'
    union all
    select
        '20260808000500',
        '20260808000500_hotmart_purchase_semantic_conflicts.sql',
        (to_regclass('public.hotmart_purchase_semantic_conflicts') is not null)::int
        + exists(select 1 from functions where proname = 'admit_hotmart_purchase_approved')::int
        + exists(select 1 from triggers where tgname = 'followup_attempts_guard_purchase_semantic_conflict')::int,
        3,
        'catalog_objects'
    union all
    select
        '20260809000100',
        '20260809000100_inbound_opt_out_durable.sql',
        (to_regclass('public.contact_opt_out_events') is not null)::int
        + exists(select 1 from functions where proname = 'apply_chatwoot_inbound_opt_out')::int
        + exists(select 1 from triggers where tgname = 'contacts_protect_authoritative_opt_out')::int,
        3,
        'catalog_objects'
    union all
    select
        '20260810000100',
        '20260810000100_lancemos_pilot_boundary.sql',
        (to_regclass('public.pilot_scope_versions') is not null)::int
        + (to_regclass('public.pilot_runtime_controls') is not null)::int
        + (to_regclass('public.pilot_outbound_request_authorizations') is not null)::int
        + exists(select 1 from functions where proname = 'authorize_lancemos_pilot_request_start')::int
        + exists(select 1 from triggers where tgname = 'pilot_outbound_authorizations_append_only')::int,
        5,
        'catalog_objects'
    union all
    select
        '20260810000200',
        '20260810000200_hotmart_cart_abandonment_authoritative.sql',
        (to_regclass('public.hotmart_cart_abandonment_semantic_conflicts') is not null)::int
        + exists(select 1 from functions where proname = 'admit_hotmart_cart_abandonment')::int
        + exists(select 1 from triggers where tgname = 'recovery_case_events_validate_hotmart_abandonment')::int,
        3,
        'catalog_objects'
    union all
    select
        '20260810000300',
        '20260810000300_lancemos_pilot_boundary_runtime.sql',
        (to_regclass('public.pilot_recovery_case_bindings') is not null)::int
        + exists(select 1 from functions where proname = 'get_lancemos_pilot_runtime_status')::int
        + exists(select 1 from functions where proname = 'plan_lancemos_pilot_cart_recovery')::int
        + exists(select 1 from triggers where tgname = 'pilot_recovery_case_bindings_append_only')::int,
        4,
        'catalog_objects'
    union all
    select
        '20260810000400',
        '20260810000400_executable_human_handoff.sql',
        (to_regclass('public.human_handoff_requests') is not null)::int
        + (to_regclass('public.human_handoff_request_evidence') is not null)::int
        + (to_regclass('public.human_handoff_projection_effects') is not null)::int
        + exists(select 1 from functions where proname = 'request_human_handoff')::int
        + exists(select 1 from functions where proname = 'claim_human_handoff_projection_effects')::int
        + exists(select 1 from triggers where tgname = 'human_handoff_projection_effects_protect_identity')::int,
        6,
        'catalog_objects'
    union all
    select
        '20260812000100',
        '20260812000100_supabase_function_acl_hardening.sql',
        (
            select (
                count(*) filter (
                    where has_function_privilege('anon', oid, 'execute')
                       or has_function_privilege('authenticated', oid, 'execute')
                ) = 0
                and count(*) filter (
                    where prorettype = 'trigger'::regtype
                      and has_function_privilege('service_role', oid, 'execute')
                ) = 0
            )::int
            from pg_proc
            where pronamespace = 'public'::regnamespace
        ),
        1,
        'effective_acl'
    union all
    select
        '20260813000100',
        '20260813000100_absolute_followup_deadlines.sql',
        (
            position(
                'min(attempt.accepted_at)'
                in pg_get_functiondef(
                    'public._finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamp with time zone,timestamp with time zone,timestamp with time zone)'::regprocedure
                )
            ) > 0
        )::int
        + (
            position(
                'v_next_due_at := v_sequence_started_at + v_next_delay'
                in pg_get_functiondef(
                    'public._finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamp with time zone,timestamp with time zone,timestamp with time zone)'::regprocedure
                )
            ) > 0
        )::int
        + (
            position(
                'v_next_due_at := p_now + v_next_delay'
                in pg_get_functiondef(
                    'public._finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamp with time zone,timestamp with time zone,timestamp with time zone)'::regprocedure
                )
            ) = 0
        )::int
        + exists(
            select 1
            from pg_trigger
            where tgrelid = 'public.followup_policy_versions'::regclass
              and tgname = 'followup_policy_step_offsets_validate'
              and not tgisinternal
        )::int
        + (
            not has_function_privilege(
                'service_role',
                'public._finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamp with time zone,timestamp with time zone,timestamp with time zone)',
                'execute'
            )
        )::int,
        5,
        'function_body_trigger_and_acl'
    union all
    select
        '20260814000100',
        '20260814000100_hotmart_purchase_worker_table_acl.sql',
        (
            select p.prosecdef::int
            from pg_proc p
            where p.oid = 'public.apply_hotmart_purchase_approved(
                uuid,text,text,text,text,text,timestamp with time zone
            )'::regprocedure
        )
        + (not has_table_privilege(
              'service_role',
              'public.followup_delivery_attempts',
              'update'
          ))::int,
        2,
        'security_definer_and_no_direct_update'
)
select
    version,
    filename,
    classification,
    present_markers,
    total_markers,
    case
        when present_markers = total_markers then 'fingerprint_present'
        when present_markers = 0 then 'fingerprint_absent'
        else 'fingerprint_partial'
    end as fingerprint_status
from fingerprints
order by version;
