-- Read-only Supabase schema fingerprint for the canonical migration stack.
-- This query inspects catalogs only. It does not prove that a migration was run,
-- nor does it read application rows or migration payloads.

with
functions as (
    select
        p.oid,
        p.proname,
        p.prosecdef,
        p.proconfig,
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
    select indexname, indexdef
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
    union all
    select
        '20260814000150',
        '20260814000150_hotmart_purchase_worker_search_path.sql',
        coalesce((
            select (
                p.prosecdef
                and array_to_string(p.proconfig, ',') =
                    'search_path=pg_catalog, public, pg_temp'
            )::int
            from pg_proc p
            where p.oid = 'public.apply_hotmart_purchase_approved(
                uuid,text,text,text,text,text,timestamp with time zone
            )'::regprocedure
        ), 0),
        1,
        'explicit_definer_search_path'
    union all
    select
        '20260814000200',
        '20260814000200_precheckout_purchase_intents.sql',
        (to_regclass('public.precheckout_submissions') is not null)::int
        + (to_regclass('public.purchase_intents') is not null)::int
        + (to_regclass('public.purchase_intent_submissions') is not null)::int
        + (to_regclass('public.precheckout_submission_conflicts') is not null)::int
        + exists(
            select 1
            from functions
            where proname = 'admit_precheckout_form_submission'
        )::int
        + coalesce((
            select (
                has_function_privilege('service_role', oid, 'execute')
                and not has_function_privilege('anon', oid, 'execute')
                and not has_function_privilege('authenticated', oid, 'execute')
            )::int
            from functions
            where proname = 'admit_precheckout_form_submission'
        ), 0)
        + coalesce((
            select (
                array_to_string(p.proconfig, ',') =
                    'search_path=pg_catalog, public, pg_temp'
            )::int
            from pg_proc p
            where p.oid = 'public.admit_precheckout_form_submission(
                text,jsonb,jsonb
            )'::regprocedure
        ), 0)
        + (
            not has_table_privilege('service_role', 'public.precheckout_submissions', 'insert')
            and not has_table_privilege('service_role', 'public.purchase_intents', 'insert')
            and not has_table_privilege('service_role', 'public.purchase_intent_submissions', 'insert')
            and not has_table_privilege('service_role', 'public.precheckout_submission_conflicts', 'insert')
        )::int,
        8,
        'catalog_objects_and_effective_acl'
    union all
    select
        '20260816000100',
        '20260816000100_commercial_case_root.sql',
        (to_regclass('public.commercial_cases') is not null)::int
        + exists(
            select 1
            from information_schema.columns c
            where c.table_schema = 'public'
              and c.table_name = 'recovery_cases'
              and c.column_name = 'commercial_case_id'
              and c.is_nullable = 'NO'
        )::int
        + (
            select (count(*) = 4)::int
            from pg_trigger t
            join pg_class relation on relation.oid = t.tgrelid
            join pg_namespace namespace on namespace.oid = relation.relnamespace
            where namespace.nspname = 'public'
              and t.tgname in (
                  'recovery_cases_bind_commercial_case_id',
                  'recovery_cases_sync_commercial_case',
                  'commercial_cases_protect_shadow',
                  'recovery_cases_validate_commercial_case_shadow'
              )
              and not t.tgisinternal
        )
        + (
            select (count(*) = 4)::int
            from functions
            where proname in (
                'bind_recovery_commercial_case_id',
                'sync_recovery_commercial_case',
                'protect_commercial_case_shadow',
                'validate_recovery_commercial_case_shadow'
            )
        )
        + (
            select (count(*) = 2)::int
            from functions
            where proname in (
                'sync_recovery_commercial_case',
                'validate_recovery_commercial_case_shadow'
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
        )
        + coalesce((
            select relation.relrowsecurity::int
            from pg_class relation
            join pg_namespace namespace on namespace.oid = relation.relnamespace
            where namespace.nspname = 'public'
              and relation.relname = 'commercial_cases'
        ), 0)
        + (
            not has_table_privilege('anon', 'public.commercial_cases', 'select')
            and not has_table_privilege('anon', 'public.commercial_cases', 'insert')
            and not has_table_privilege('anon', 'public.commercial_cases', 'update')
            and not has_table_privilege('anon', 'public.commercial_cases', 'delete')
            and not has_table_privilege('authenticated', 'public.commercial_cases', 'select')
            and not has_table_privilege('authenticated', 'public.commercial_cases', 'insert')
            and not has_table_privilege('authenticated', 'public.commercial_cases', 'update')
            and not has_table_privilege('authenticated', 'public.commercial_cases', 'delete')
            and not has_table_privilege('service_role', 'public.commercial_cases', 'select')
            and not has_table_privilege('service_role', 'public.commercial_cases', 'insert')
            and not has_table_privilege('service_role', 'public.commercial_cases', 'update')
            and not has_table_privilege('service_role', 'public.commercial_cases', 'delete')
            and not has_function_privilege(
                'anon', 'public.bind_recovery_commercial_case_id()', 'execute'
            )
            and not has_function_privilege(
                'anon', 'public.sync_recovery_commercial_case()', 'execute'
            )
            and not has_function_privilege(
                'anon', 'public.protect_commercial_case_shadow()', 'execute'
            )
            and not has_function_privilege(
                'anon', 'public.validate_recovery_commercial_case_shadow()', 'execute'
            )
            and not has_function_privilege(
                'authenticated', 'public.bind_recovery_commercial_case_id()', 'execute'
            )
            and not has_function_privilege(
                'authenticated', 'public.sync_recovery_commercial_case()', 'execute'
            )
            and not has_function_privilege(
                'authenticated', 'public.protect_commercial_case_shadow()', 'execute'
            )
            and not has_function_privilege(
                'authenticated', 'public.validate_recovery_commercial_case_shadow()', 'execute'
            )
            and not has_function_privilege(
                'service_role', 'public.bind_recovery_commercial_case_id()', 'execute'
            )
            and not has_function_privilege(
                'service_role', 'public.sync_recovery_commercial_case()', 'execute'
            )
            and not has_function_privilege(
                'service_role', 'public.protect_commercial_case_shadow()', 'execute'
            )
            and not has_function_privilege(
                'service_role', 'public.validate_recovery_commercial_case_shadow()', 'execute'
            )
        )::int,
        7,
        'shadow_root_and_closed_acl'
    union all
    select
        '20260816000200',
        '20260816000200_inbound_commercial_case_draft_only.sql',
        (to_regclass('public.inbound_commercial_scope_versions') is not null)::int
        + (to_regclass('public.inbound_commercial_case_admissions') is not null)::int
        + (to_regclass('public.inbound_commercial_case_conflicts') is not null)::int
        + (to_regclass('public.commercial_case_intent_correlations') is not null)::int
        + exists(
            select 1 from indexes
            where indexname = 'commercial_cases_live_inbound_conversation_scope_idx'
              and indexdef like '%conversation_id, inbound_scope_key, inbound_scope_version, product_ref%'
        )::int
        + (
            select (count(*) = 3)::int
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'commercial_cases'
              and column_name in (
                  'inbound_scope_key', 'inbound_scope_version', 'tenant_ref'
              )
        )
        + coalesce((
            select (
                p.prosecdef
                and array_to_string(p.proconfig, ',') =
                    'search_path=pg_catalog, public, pg_temp'
                and has_function_privilege('service_role', p.oid, 'execute')
                and not has_function_privilege('anon', p.oid, 'execute')
                and not has_function_privilege('authenticated', p.oid, 'execute')
            )::int
            from pg_proc p
            where p.oid = 'public.admit_inbound_commercial_case(
                text,integer,bigint,text
            )'::regprocedure
        ), 0)
        + (
            not has_table_privilege('service_role', 'public.inbound_commercial_case_admissions', 'insert')
            and not has_table_privilege('service_role', 'public.inbound_commercial_case_admissions', 'update')
            and not has_table_privilege('service_role', 'public.inbound_commercial_case_admissions', 'delete')
            and not has_table_privilege('anon', 'public.inbound_commercial_case_admissions', 'select')
            and not has_table_privilege('authenticated', 'public.inbound_commercial_case_admissions', 'select')
        )::int,
        8,
        'draft_only_canonical_admission_scoped_root_conflicts_correlation_and_acl'
    union all
    select
        '20260818000100',
        '20260818000100_precheckout_test_first_touch.sql',
        (to_regclass('public.precheckout_test_first_touch_commands') is not null)::int
        + (
            select (count(*) = 2)::int
            from functions
            where proname in (
                'begin_precheckout_test_first_touch',
                'finish_precheckout_test_first_touch'
            )
        )
        + (
            select (count(*) = 2)::int
            from functions
            where proname in (
                'begin_precheckout_test_first_touch',
                'finish_precheckout_test_first_touch'
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
        )
        + (
            has_function_privilege(
                'service_role',
                'public.begin_precheckout_test_first_touch(text,uuid,text,bigint,bigint)',
                'execute'
            )
            and has_function_privilege(
                'service_role',
                'public.finish_precheckout_test_first_touch(uuid,text,bigint,bigint,text)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.begin_precheckout_test_first_touch(text,uuid,text,bigint,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.finish_precheckout_test_first_touch(uuid,text,bigint,bigint,text)',
                'execute'
            )
        )::int
        + (
            not has_table_privilege(
                'service_role',
                'public.precheckout_test_first_touch_commands',
                'select'
            )
            and not has_table_privilege(
                'service_role',
                'public.precheckout_test_first_touch_commands',
                'insert'
            )
            and not has_table_privilege(
                'anon',
                'public.precheckout_test_first_touch_commands',
                'select'
            )
            and not has_table_privilege(
                'authenticated',
                'public.precheckout_test_first_touch_commands',
                'select'
            )
        )::int,
        5,
        'one_shot_test_only_at_most_once_and_closed_acl'
    union all
    select
        '20260818000200',
        '20260818000200_observed_lead_precheckout.sql',
        (
            select (count(*) = 1)::int
            from functions
            where proname = 'admit_observed_lead_precheckout'
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
        )
        + (
            has_function_privilege(
                'service_role',
                'public.admit_observed_lead_precheckout(text,jsonb,jsonb)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.admit_observed_lead_precheckout(text,jsonb,jsonb)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.admit_observed_lead_precheckout(text,jsonb,jsonb)',
                'execute'
            )
        )::int
        + (
            select (is_nullable = 'YES')::int
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'purchase_intents'
              and column_name = 'normalized_phone'
        )
        + (to_regclass('public.purchase_intents_one_observed_email_idx') is not null)::int,
        4,
        'observed_intent_only_nullable_phone_and_closed_rpc_acl'
    union all
    select
        '20260820000100',
        '20260820000100_hotmart_purchase_intent_correlation.sql',
        (to_regclass('public.hotmart_purchase_intent_scopes') is not null)::int
        + (to_regclass('public.hotmart_purchase_intent_event_identities') is not null)::int
        + (to_regclass('public.hotmart_purchase_intent_correlations') is not null)::int
        + (to_regclass('public.hotmart_purchase_intent_correlation_candidates') is not null)::int
        + exists(
            select 1 from functions
            where proname in (
                'correlate_hotmart_purchase_intent',
                'admit_and_correlate_hotmart_purchase_approved',
                'admit_and_correlate_hotmart_cart_abandonment'
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
            having count(*) = 3
        )::int,
        5,
        'atomic_canonical_hotmart_intent_correlation_without_effects'
    union all
    select
        '20260820000200',
        '20260820000200_hotmart_intent_base_search_path.sql',
        exists(
            select 1 from functions
            where oid in (
                to_regprocedure(
                    'public._admit_hotmart_purchase_approved_base(text,jsonb)'
                ),
                to_regprocedure(
                    'public._admit_hotmart_cart_abandonment_base(text,jsonb)'
                )
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
            having count(*) = 2
        )::int,
        1,
        'owner_only_hotmart_admission_bases_catalog_first_search_path'
    union all
    select
        '20260820000300',
        '20260820000300_hotmart_confirmed_abandonment.sql',
        exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.correlate_hotmart_purchase_intent(uuid)'
            )
              and position('confirmed_abandonment' in definition) > 0
              and position('abandonment_candidate' in definition) = 0
        )::int
        + exists(
            select 1 from pg_constraint constraint_row
            where constraint_row.conrelid = 'public.purchase_intents'::regclass
              and constraint_row.contype = 'c'
              and position(
                  'current_classification' in
                  pg_get_constraintdef(constraint_row.oid)
              ) > 0
              and position(
                  'confirmed_abandonment' in
                  pg_get_constraintdef(constraint_row.oid)
              ) > 0
              and position(
                  'abandonment_candidate' in
                  pg_get_constraintdef(constraint_row.oid)
              ) = 0
        )::int,
        2,
        'authoritative_confirmed_abandonment_classification'
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
