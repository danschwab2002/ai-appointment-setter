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
        p.proacl,
        p.proowner,
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
    union all
    select
        '20260820000400',
        '20260820000400_hotmart_intent_correlation_contract.sql',
        (
            not has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_hotmart_purchase_approved(text,jsonb)'
                ),
                'execute'
            )
            and not has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_hotmart_cart_abandonment(text,jsonb)'
                ),
                'execute'
            )
        )::int
        + (
            has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_and_correlate_hotmart_purchase_approved(text,jsonb,text,text)'
                ),
                'execute'
            )
            and not has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)'
                ),
                'execute'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_johanna_hotmart_cart_abandonment(text,jsonb,text,text)'
                ),
                'execute'
            )
        )::int,
        2,
        'legacy_hotmart_shims_revoked_scope_fixed_wrappers_preserved'
    union all
    select
        '20260821000100',
        '20260821000100_hotmart_abandonment_timer.sql',
        (to_regclass('public.hotmart_abandonment_timer_policy_bindings') is not null)::int
        + (to_regclass('public.hotmart_abandonment_timer_policy_binding_events') is not null)::int
        + (to_regclass('public.hotmart_abandonment_reevaluations') is not null)::int
        + (to_regclass('public.hotmart_abandonment_reevaluation_events') is not null)::int
        + exists(
            select 1 from triggers
            where tgname = 'hotmart_abandonment_reevaluation_events_append_only'
        )::int
        + exists(
            select 1 from functions
            where oid in (
                to_regprocedure(
                    'public.schedule_hotmart_abandonment_reevaluation(uuid)'
                ),
                to_regprocedure(
                    'public.list_due_hotmart_abandonment_reevaluations(timestamp with time zone,integer)'
                ),
                to_regprocedure(
                    'public.reevaluate_hotmart_abandonment_timer(uuid,timestamp with time zone)'
                )
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
            having count(*) = 3
        )::int
        + (
            has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.list_due_hotmart_abandonment_reevaluations(timestamp with time zone,integer)'
                ),
                'execute'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.reevaluate_hotmart_abandonment_timer(uuid,timestamp with time zone)'
                ),
                'execute'
            )
            and not has_function_privilege(
                'anon',
                to_regprocedure(
                    'public.list_due_hotmart_abandonment_reevaluations(timestamp with time zone,integer)'
                ),
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                to_regprocedure(
                    'public.reevaluate_hotmart_abandonment_timer(uuid,timestamp with time zone)'
                ),
                'execute'
            )
        )::int,
        7,
        'versioned_producer_delay_snapshot_and_db_only_reevaluation'
    union all
    select
        '20260821000200',
        '20260821000200_lead_whatsapp_consent_authorization.sql',
        exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.admit_observed_lead_precheckout(text,jsonb,jsonb)'
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
              and position('1.1.0' in definition) > 0
              and position(
                  'johanna-precheckout-whatsapp-disclosure-v1' in definition
              ) > 0
        )::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.correlate_hotmart_purchase_intent(uuid)'
            )
              and position(
                  'set current_classification = ''confirmed_abandonment'', updated_at = clock_timestamp()'
                  in regexp_replace(definition, '[[:space:]]+', ' ', 'g')
              ) > 0
        )::int
        + coalesce((
            select (
                has_function_privilege('service_role', oid, 'execute')
                and not has_function_privilege('anon', oid, 'execute')
                and not has_function_privilege('authenticated', oid, 'execute')
            )::int
            from functions
            where oid = to_regprocedure(
                'public.admit_observed_lead_precheckout(text,jsonb,jsonb)'
            )
        ), 0),
        3,
        'versioned_landing_consent_preserved_for_internal_reevaluation'
    union all
    select
        '20260823000100',
        '20260823000100_inbound_durable_handoff.sql',
        exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.request_inbound_human_handoff(uuid,text,text,text,integer,timestamp with time zone)'
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
        )::int
        + exists(
            select 1 from triggers
            where tgname = 'human_handoff_requests_bind_commercial_case'
        )::int
        + exists(
            select 1 from indexes
            where indexname =
                'human_handoff_requests_one_live_per_commercial_case_idx'
        )::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.claim_human_handoff_projection_effects(text,integer,integer,timestamp with time zone)'
            )
              and position('inbound_commercial_case_admissions' in definition) > 0
        )::int,
        4,
        'commercial_case_root_inbound_handoff_and_projection'
    union all
    select
        '20260824000100',
        '20260824000100_operator_correlation_review_read.sql',
        (
            select count(*)::int
            from functions
            where oid in (
                to_regprocedure(
                    'public.list_operator_unresolved_correlations(text,text,integer,uuid)'
                ),
                to_regprocedure(
                    'public.get_operator_unresolved_correlation(text,text,uuid)'
                )
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
        )
        + (
            has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.list_operator_unresolved_correlations(text,text,integer,uuid)'
                ),
                'execute'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.get_operator_unresolved_correlation(text,text,uuid)'
                ),
                'execute'
            )
            and not has_function_privilege(
                'anon',
                to_regprocedure(
                    'public.list_operator_unresolved_correlations(text,text,integer,uuid)'
                ),
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                to_regprocedure(
                    'public.get_operator_unresolved_correlation(text,text,uuid)'
                ),
                'execute'
            )
        )::int
        + (
            not has_table_privilege(
                'service_role', 'public.purchase_intents', 'select'
            )
            and not has_table_privilege(
                'service_role',
                'public.hotmart_purchase_intent_correlations',
                'select'
            )
        )::int,
        4,
        'scoped_masked_operator_read_without_direct_table_access'
    union all
    select
        '20260824000200',
        '20260824000200_operator_correlation_manual_resolution.sql',
        (to_regclass('public.operator_correlation_resolution_commands') is not null)::int
        + (to_regclass('public.operator_correlation_resolutions') is not null)::int
        + (
            select count(*)::int
            from functions
            where oid in (
                to_regprocedure(
                    'public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)'
                ),
                to_regprocedure(
                    'public.confirm_operator_correlation_resolution(text,text,text,uuid,text,uuid)'
                )
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
        )
        + (
            has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)'
                ),
                'execute'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.confirm_operator_correlation_resolution(text,text,text,uuid,text,uuid)'
                ),
                'execute'
            )
            and not has_function_privilege(
                'anon',
                to_regprocedure(
                    'public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)'
                ),
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                to_regprocedure(
                    'public.confirm_operator_correlation_resolution(text,text,text,uuid,text,uuid)'
                ),
                'execute'
            )
        )::int
        + (
            not has_table_privilege(
                'service_role',
                'public.operator_correlation_resolution_commands',
                'select,insert,update,delete'
            )
            and not has_table_privilege(
                'service_role',
                'public.operator_correlation_resolutions',
                'select,insert,update,delete'
            )
        )::int
        + coalesce((
            select (
                not command_guard.prosecdef
                and array_to_string(command_guard.proconfig, ',') =
                    'search_path=pg_catalog, public, pg_temp'
                and not has_function_privilege(
                    'anon', command_guard.oid, 'execute'
                )
                and not has_function_privilege(
                    'authenticated', command_guard.oid, 'execute'
                )
                and not has_function_privilege(
                    'service_role', command_guard.oid, 'execute'
                )
                and exists (
                    select 1
                    from triggers
                    where tgname =
                        'validate_operator_correlation_command_before_insert'
                )
            )::int
            from functions command_guard
            where command_guard.oid = to_regprocedure(
                'public.validate_operator_correlation_resolution_command_insert()'
            )
        ), 0),
        7,
        'supervised_manual_resolution_overlay_without_direct_table_access'
    union all
    select
        '20260825000100',
        '20260825000100_proactive_lead_identity_bootstrap.sql',
        (to_regclass('public.proactive_lead_bootstrap_targets') is not null)::int
        + (to_regclass('public.proactive_lead_identity_bootstrap_commands') is not null)::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.bootstrap_proactive_lead_identity(text,uuid,text,integer,bigint,text,text)'
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
        )::int
        + coalesce((
            select (
                has_function_privilege('service_role', oid, 'execute')
                and not has_function_privilege('anon', oid, 'execute')
                and not has_function_privilege('authenticated', oid, 'execute')
            )::int
            from functions
            where oid = to_regprocedure(
                'public.bootstrap_proactive_lead_identity(text,uuid,text,integer,bigint,text,text)'
            )
        ), 0),
        4,
        'effect_free_proactive_identity_bootstrap'
    union all
    select
        '20260825000200',
        '20260825000200_johanna_abandonment_one_shot.sql',
        (to_regclass('public.johanna_abandonment_one_shot_commands') is not null)::int
        + (
            select (count(*) = 2)::int
            from functions
            where oid in (
                to_regprocedure(
                    'public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint)'
                ),
                to_regprocedure(
                    'public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text)'
                )
            )
        )
        + (
            select (count(*) = 2)::int
            from functions
            where oid in (
                to_regprocedure(
                    'public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint)'
                ),
                to_regprocedure(
                    'public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text)'
                )
            )
              and prosecdef
              and array_to_string(proconfig, ',') =
                  'search_path=pg_catalog, public, pg_temp'
        )
        + (
            has_function_privilege(
                'service_role',
                'public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint)',
                'execute'
            )
            and has_function_privilege(
                'service_role',
                'public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.begin_johanna_abandonment_one_shot(text,uuid,text,bigint,bigint,text,integer,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.finish_johanna_abandonment_one_shot(uuid,text,bigint,bigint,text)',
                'execute'
            )
        )::int
        + (
            not has_table_privilege(
                'service_role',
                'public.johanna_abandonment_one_shot_commands',
                'select'
            )
            and not has_table_privilege(
                'anon',
                'public.johanna_abandonment_one_shot_commands',
                'select'
            )
            and not has_table_privilege(
                'authenticated',
                'public.johanna_abandonment_one_shot_commands',
                'select'
            )
        )::int,
        5,
        'johanna_v1_1_single_budget_template_command_closed_acl'
    union all
    select
        '20260825000300',
        '20260825000300_reconcile_johanna_abandonment_one_shot.sql',
        (to_regprocedure(
            'public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint)'
        ) is not null)::int
        + coalesce((
            select (
                prosecdef
                and array_to_string(proconfig, ',') =
                    'search_path=pg_catalog, public, pg_temp'
            )::int
            from functions
            where oid = to_regprocedure(
                'public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint)'
            )
        ), 0)
        + (
            has_function_privilege(
                'service_role',
                'public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint)',
                'execute'
            )
        )::int,
        3,
        'reconcile_observed_one_shot_without_resend'
    union all
    select
        '20260825000400',
        '20260825000400_johanna_waba_single_touch_policy.sql',
        exists (
            select 1
            from public.followup_policy_versions
            where policy_key = 'johanna-abandonment-single-touch-e2e'
              and version = 2
              and status = 'published'
              and purpose = 'cart_recovery'
              and max_automatic_messages = 1
              and steps =
                  '[{"step_key":"first_contact","mode":"approved_template"}]'::jsonb
        )::int
        + exists (
            select 1
            from public.pilot_scope_versions
            where scope_key = 'johanna-abandonment-template-e2e'
              and version = 2
              and status = 'published'
              and policy_key = 'johanna-abandonment-single-touch-e2e'
              and policy_version = 2
              and channel_provider = 'waba'
              and chatwoot_account_id = 1
              and chatwoot_inbox_id = 9
              and source_event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
              and external_product_id = '8104005'
              and offer_code = 'bxjge6zq'
              and max_cohort_contacts = 1
              and max_outbound_request_starts_total = 1
              and max_outbound_request_starts_per_day = 1
        )::int
        + exists (
            select 1
            from public.pilot_runtime_controls
            where scope_key = 'johanna-abandonment-template-e2e'
              and scope_version = 2
              and runtime_state = 'inactive'
              and generation = 1
        )::int
        + (to_regprocedure(
            'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)'
        ) is not null)::int
        + coalesce((
            select (
                prosecdef
                and proconfig @> array[
                    'search_path=pg_catalog, public, pg_temp'
                ]
            )::int
            from functions
            where oid = to_regprocedure(
                'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)'
            )
        ), 0)
        + (
            has_function_privilege(
                'service_role',
                'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)',
                'execute'
            )
        )::int,
        6,
        'johanna_waba_policy_scope_runtime_and_hotmart_auto_v2'
    union all
    select
        '20260825000500',
        '20260825000500_johanna_mvp_full_activation.sql',
        (to_regclass('public.johanna_payment_failure_cases') is not null)::int
        + (to_regprocedure(
            'public.admit_johanna_payment_failure(text,jsonb,text,text)'
        ) is not null)::int
        + coalesce((
            select (
                prosecdef
                and proconfig @> array[
                    'search_path=pg_catalog, public, pg_temp'
                ]
            )::int
            from functions
            where oid = to_regprocedure(
                'public.admit_johanna_payment_failure(text,jsonb,text,text)'
            )
        ), 0)
        + (
            has_function_privilege(
                'service_role',
                'public.admit_johanna_payment_failure(text,jsonb,text,text)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.admit_johanna_payment_failure(text,jsonb,text,text)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.admit_johanna_payment_failure(text,jsonb,text,text)',
                'execute'
            )
        )::int
        + (
            pg_get_function_result(to_regprocedure(
                'public.claim_human_handoff_projection_effects(text,integer,integer,timestamp with time zone)'
            )) like '%external_user_id text%'
        )::int,
        5,
        'payment_failure_review_and_scoped_handoff_identity'
    union all
    select
        '20260826000100',
        '20260826000100_johanna_dynamic_recipients.sql',
        (to_regprocedure(
            'public.begin_johanna_abandonment_hotmart_auto_v2(text,uuid,uuid,bigint,bigint,text,integer,bigint)'
        ) is not null)::int
        + coalesce((
            select (
                prosecdef
                and proconfig @> array[
                    'search_path=pg_catalog, public, pg_temp'
                ]
                and position('intent.normalized_phone' in definition) > 0
            )::int
            from functions
            where oid = to_regprocedure(
                'public.begin_johanna_abandonment_hotmart_auto_v2(text,uuid,uuid,bigint,bigint,text,integer,bigint)'
            )
        ), 0)
        + (
            has_function_privilege(
                'service_role',
                'public.begin_johanna_abandonment_hotmart_auto_v2(text,uuid,uuid,bigint,bigint,text,integer,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.begin_johanna_abandonment_hotmart_auto_v2(text,uuid,uuid,bigint,bigint,text,integer,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.begin_johanna_abandonment_hotmart_auto_v2(text,uuid,uuid,bigint,bigint,text,integer,bigint)',
                'execute'
            )
        )::int,
        3,
        'durable_dynamic_johanna_recipient'
    union all
    select
        '20260826000200',
        '20260826000200_inbound_paused_replay_guard.sql',
        (to_regprocedure(
            'public.admit_inbound_commercial_case_v2(text,integer,bigint,text)'
        ) is not null)::int
        + coalesce((
            select (
                prosecdef
                and proconfig @> array[
                    'search_path=pg_catalog, public, pg_temp'
                ]
                and position('outcome := ''blocked''' in definition) > 0
                and position('not v_conversation.human_takeover' in definition) > 0
            )::int
            from functions
            where oid = to_regprocedure(
                'public.admit_inbound_commercial_case_v2(text,integer,bigint,text)'
            )
        ), 0)
        + (
            has_function_privilege(
                'service_role',
                'public.admit_inbound_commercial_case_v2(text,integer,bigint,text)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.admit_inbound_commercial_case_v2(text,integer,bigint,text)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.admit_inbound_commercial_case_v2(text,integer,bigint,text)',
                'execute'
            )
        )::int
        + (
            not has_function_privilege(
                'service_role',
                'public.admit_inbound_commercial_case_base(text,integer,bigint,text)',
                'execute'
            )
        )::int,
        4,
        'inbound_replay_revalidates_durable_stop'
    union all
    select
        '20260827000100',
        '20260827000100_hotmart_canceled_any_reason.sql',
        exists(
            select 1
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'johanna_payment_failure_cases'
              and column_name = 'refusal_reason'
              and is_nullable = 'YES'
        )::int
        + coalesce((
            select (
                prosecdef
                and proconfig @> array[
                    'search_path=pg_catalog, public, pg_temp'
                ]
                and position(
                    'purchase,status}'' is distinct from ''CANCELED'''
                    in definition
                ) > 0
                and position('NO_FUNDS' in definition) = 0
            )::int
            from functions
            where oid = to_regprocedure(
                'public.admit_johanna_payment_failure(text,jsonb,text,text)'
            )
        ), 0)
        + coalesce((
            select (
                prosecdef
                and proconfig @> array[
                    'search_path=pg_catalog, public, pg_temp'
                ]
                and position(
                    'failure_case.purchase_status <> ''CANCELED'''
                    in definition
                ) > 0
                and position('failure_case.refusal_reason <>' in definition) = 0
            )::int
            from functions
            where oid = to_regprocedure(
                'public.begin_johanna_payment_failure_hotmart_auto(text,uuid,bigint,bigint)'
            )
        ), 0)
        + (
            has_function_privilege(
                'service_role',
                'public.admit_johanna_payment_failure(text,jsonb,text,text)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.admit_johanna_payment_failure(text,jsonb,text,text)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.admit_johanna_payment_failure(text,jsonb,text,text)',
                'execute'
            )
        )::int,
        4,
        'hotmart_canceled_status_with_optional_refusal_reason'
    union all
    select
        '20260827000200',
        '20260827000200_chatwoot_invalid_contact_retry.sql',
        exists(
            select 1
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'johanna_abandonment_one_shot_commands'
              and column_name = 'invalid_contact_retry_count'
              and is_nullable = 'NO'
        )::int
        + coalesce((
            select (
                prosecdef
                and proconfig @> array[
                    'search_path=pg_catalog, public, pg_temp'
                ]
                and position('invalid_contact_id' in definition) > 0
                and position('invalid_contact_retry_count <> 0' in definition) > 0
                and position('''retry_started''::text' in definition) > 0
            )::int
            from functions
            where oid = to_regprocedure(
                'public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint)'
            )
        ), 0)
        + (
            has_function_privilege(
                'service_role',
                'public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'anon',
                'public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint)',
                'execute'
            )
            and not has_function_privilege(
                'authenticated',
                'public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint)',
                'execute'
            )
        )::int,
        3,
        'single_bounded_invalid_contact_retry'
    union all
    select
        '20260828000100',
        '20260828000100_operator_correlation_product_casefold.sql',
        coalesce((
            select (
                count(*) = 4
                and sum(
                    (
                        length(definition)
                        - length(replace(
                            definition,
                            'lower(intent.product_ref) = lower(',
                            ''
                        ))
                    ) / length('lower(intent.product_ref) = lower(')
                ) = 6
            )::int
            from functions
            where oid in (
                to_regprocedure(
                    'public.validate_operator_correlation_resolution_command_insert()'
                ),
                to_regprocedure(
                    'public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)'
                ),
                to_regprocedure(
                    'public.confirm_operator_correlation_resolution(text,text,text,uuid,text,uuid)'
                ),
                to_regprocedure(
                    'public.list_operator_unresolved_correlations(text,text,integer,uuid)'
                )
            )
        ), 0)
        + (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.validate_operator_correlation_resolution_command_insert()'
                )
                  and not prosecdef
                  and proconfig @> array[
                      'search_path=pg_catalog, public, pg_temp'
                  ]
            )
            and (
                select count(*) = 3
                from functions
                where oid in (
                    to_regprocedure(
                        'public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)'
                    ),
                    to_regprocedure(
                        'public.confirm_operator_correlation_resolution(text,text,text,uuid,text,uuid)'
                    ),
                    to_regprocedure(
                        'public.list_operator_unresolved_correlations(text,text,integer,uuid)'
                    )
                )
                  and prosecdef
                  and proconfig @> array[
                      'search_path=pg_catalog, public, pg_temp'
                  ]
            )
        )::int,
        2,
        'operator_correlation_product_scope_casefolded'
    union all
    select
        '20260829000100',
        '20260829000100_johanna_operator_resolution_one_shot.sql',
        (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)'
                )
                  and prosecdef
                  and proconfig @> array[
                      'search_path=pg_catalog, public, pg_temp'
                  ]
            )
        )::int
        + coalesce((
            select (
                (
                    length(definition)
                    - length(replace(
                        definition,
                        'operator_resolution_authorized',
                        ''
                    ))
                ) / length('operator_resolution_authorized') = 5
                and position(
                    'resolution.resolution_outcome = ''linked_candidate'''
                    in definition
                ) > 0
                and position(
                    'correlation.reason_code = ''email_phone_conflict'''
                    in definition
                ) > 0
                and position(
                    'intent.current_classification = ''identity_conflict'''
                    in definition
                ) > 0
                and position(
                    'not intent.whatsapp_contact_authorized'
                    in definition
                ) > 0
                and position(
                    'from public.contact_opt_out_events stop'
                    in definition
                ) > 0
            )::int
            from functions
            where oid = to_regprocedure(
                'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)'
            )
        ), 0),
        2,
        'operator_resolution_authorizes_exact_one_shot_candidate'
    union all
    select
        '20260829000200',
        '20260829000200_precheckout_delayed_first_touch_timer.sql',
        (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.schedule_precheckout_first_touch_reevaluation(uuid,uuid)'
                )
                  and prosecdef
                  and proconfig @> array[
                      'search_path=pg_catalog, public, pg_temp'
                  ]
                  and position(
                      'precheckout_first_touch_enabled'
                      in definition
                  ) > 0
                  and position(
                      'v_delay_seconds <> 3600'
                      in definition
                  ) > 0
            )
        )::int
        + (
            exists(
                select 1
                from pg_attribute attribute
                where attribute.attrelid =
                    'public.hotmart_abandonment_reevaluations'::regclass
                  and attribute.attname = 'source_kind'
                  and not attribute.attisdropped
            )
            and exists(
                select 1
                from pg_attribute attribute
                where attribute.attrelid =
                    'public.hotmart_abandonment_reevaluations'::regclass
                  and attribute.attname = 'source_submission_id'
                  and not attribute.attisdropped
            )
        )::int
        + (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.admit_observed_lead_precheckout(text,jsonb,jsonb)'
                )
                  and position(
                      'schedule_precheckout_first_touch_reevaluation'
                      in definition
                  ) > 0
            )
            and exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.list_due_hotmart_abandonment_reevaluations(timestamptz,integer)'
                )
                  and position(
                      'reevaluation.source_kind = ''hotmart_event'''
                      in definition
                  ) > 0
            )
        )::int,
        3,
        'precheckout_delayed_timer_durable_default_off'
    union all
    select
        '20260829000300',
        '20260829000300_precheckout_delayed_one_shot_reservation.sql',
        (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)'
                )
                  and prosecdef
                  and proconfig @> array[
                      'search_path=pg_catalog, public, pg_temp'
                  ]
                  and position(
                      'insert into public.johanna_abandonment_one_shot_commands'
                      in definition
                  ) > 0
                  and position(
                      'johanna-abandonment-template-e2e-v2'
                      in definition
                  ) > 0
                  and position(
                      'johanna-recovery-budget:'
                      in definition
                  ) > 0
            )
        )::int
        + (
            exists(
                select 1
                from pg_attribute attribute
                where attribute.attrelid =
                    'public.johanna_abandonment_one_shot_commands'::regclass
                  and attribute.attname = 'source_reevaluation_id'
                  and not attribute.attisdropped
            )
        )::int
        + (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.reevaluate_hotmart_abandonment_timer(uuid,timestamptz)'
                )
                  and position(
                      '_reevaluate_precheckout_delayed_first_touch'
                      in definition
                  ) > 0
            )
        )::int
        + (
            exists(
                select 1
                from pg_indexes index_row
                where index_row.schemaname = 'public'
                  and index_row.indexname =
                      'johanna_abandonment_one_shot_commands_target_phone_idx'
                  and index_row.indexdef like '%UNIQUE%target_phone%'
            )
        )::int,
        4,
        'precheckout_delayed_reserves_shared_one_shot_budget'
    union all
    select
        '20260829000400',
        '20260829000400_precheckout_delayed_worker_sender.sql',
        (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)'
                )
                  and prosecdef
                  and proconfig @> array[
                      'search_path=pg_catalog, public, pg_temp'
                  ]
                  and position('p_include_precheckout' in definition) > 0
                  and position('precheckout_intent' in definition) > 0
            )
        )::int
        + (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.get_precheckout_delayed_one_shot_command(uuid)'
                )
                  and prosecdef
                  and proconfig @> array[
                      'search_path=pg_catalog, public, pg_temp'
                  ]
                  and position('source_reevaluation_id' in definition) > 0
                  and position('intent_submission.purchase_intent_id' in definition) > 0
                  and position('submitted_at' in definition) > 0
                  and position('send_authorized' in definition) > 0
                  and position('cancelled_purchased' in definition) > 0
                  and position(
                      'johanna_interes_precheckout_01' in definition
                  ) > 0
            )
        )::int
        + (
            not has_function_privilege(
                'public',
                'public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)',
                'EXECUTE'
            )
            and not has_function_privilege(
                'public',
                'public.get_precheckout_delayed_one_shot_command(uuid)',
                'EXECUTE'
            )
        )::int,
        3,
        'precheckout_delayed_worker_sender_default_off'
    union all
    select
        '20260829000500',
        '20260829000500_precheckout_production_readiness.sql',
        (
            exists(
                select 1 from functions
                where oid = to_regprocedure(
                    'public.get_precheckout_delayed_first_touch_readiness()'
                )
                  and prosecdef
                  and proconfig @> array[
                      'search_path=pg_catalog, public, pg_temp'
                  ]
                  and position('migration_tracking_incomplete' in definition) > 0
                  and position('precheckout_first_touch_ready' in definition) > 0
            )
        )::int
        + (
            exists(
                select 1 from public.pilot_scope_versions scope
                where scope.scope_key =
                        'johanna-precheckout-delayed-first-touch'
                  and scope.version = 1
                  and scope.status = 'published'
                  and scope.max_cohort_contacts = 1
                  and scope.max_outbound_request_starts_total = 1
                  and scope.max_outbound_request_starts_per_day = 1
            )
        )::int
        + (
            exists(
                select 1 from public.pilot_runtime_controls runtime
                where runtime.scope_key =
                        'johanna-precheckout-delayed-first-touch'
                  and runtime.scope_version = 1
                  and runtime.runtime_state = 'inactive'
                  and runtime.generation = 0
            )
        )::int
        + (
            exists(
                select 1
                from public.hotmart_abandonment_timer_policy_bindings binding
                join public.followup_policy_versions policy
                  on policy.policy_key = binding.policy_key
                 and policy.version = binding.policy_version
                where binding.tenant_ref = 'lancemos'
                  and binding.funnel_ref = 'psicologajohanna'
                  and lower(binding.product_ref) = lower('F106691755G')
                  and binding.offer_ref = 'bxjge6zq'
                  and binding.enabled
                  and not binding.precheckout_first_touch_enabled
                  and binding.policy_key =
                        'johanna-precheckout-delayed-first-touch-timer'
                  and binding.policy_version = 1
                  and policy.status = 'published'
                  and policy.grace_period = interval '60 minutes'
            )
        )::int
        + (
            exists(
                select 1 from public.followup_policy_versions policy
                where policy.policy_key =
                        'johanna-precheckout-delayed-first-touch-timer'
                  and policy.version = 1
                  and policy.status = 'published'
                  and policy.grace_period = interval '60 minutes'
            )
        )::int
        + (
            not has_function_privilege(
                'public',
                'public.get_precheckout_delayed_first_touch_readiness()',
                'EXECUTE'
            )
            and has_function_privilege(
                'service_role',
                'public.get_precheckout_delayed_first_touch_readiness()',
                'EXECUTE'
            )
        )::int,
        6,
        'precheckout_production_readiness_default_off'
    union all
    select
        '20260901000100',
        '20260901000100_commercial_ally_portability.sql',
        (to_regclass('public.commercial_ally_runtime_bindings') is not null)::int
        + exists(
            select 1 from indexes
            where indexname = 'commercial_ally_runtime_bindings_one_active'
        )::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                    'public.resolve_commercial_ally_runtime_binding(text,text,integer)'
                )
              and not prosecdef
              and proconfig @> array['search_path=""']
        )::int
        + (
            to_regprocedure(
                'public.resolve_commercial_ally_runtime_binding(text,text,integer)'
            ) is not null
            and not has_function_privilege(
                'public',
                to_regprocedure(
                    'public.resolve_commercial_ally_runtime_binding(text,text,integer)'
                ),
                'EXECUTE'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.resolve_commercial_ally_runtime_binding(text,text,integer)'
                ),
                'EXECUTE'
            )
        )::int
        + (
            to_regclass('public.commercial_ally_runtime_bindings') is not null
            and has_table_privilege(
                'service_role',
                to_regclass('public.commercial_ally_runtime_bindings'),
                'select'
            )
            and not has_table_privilege(
                'service_role',
                to_regclass('public.commercial_ally_runtime_bindings'),
                'insert'
            )
            and not has_table_privilege(
                'service_role',
                to_regclass('public.commercial_ally_runtime_bindings'),
                'update'
            )
            and not has_table_privilege(
                'service_role',
                to_regclass('public.commercial_ally_runtime_bindings'),
                'delete'
            )
            and not has_table_privilege(
                'service_role',
                to_regclass('public.commercial_ally_runtime_bindings'),
                'truncate'
            )
            and not has_table_privilege(
                'service_role',
                to_regclass('public.commercial_ally_runtime_bindings'),
                'references'
            )
            and not has_table_privilege(
                'service_role',
                to_regclass('public.commercial_ally_runtime_bindings'),
                'trigger'
            )
        )::int,
        5,
        'commercial_ally_binding_default_off'
    union all
    select
        '20260901000200',
        '20260901000200_commercial_ally_portable_precheckout.sql',
        exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.admit_portable_observed_lead_precheckout(text,text,integer,text,jsonb,jsonb)'
            )
              and prosecdef
              and proconfig @> array['search_path=pg_catalog, public, pg_temp']
        )::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.admit_portable_observed_lead_precheckout(text,text,integer,text,jsonb,jsonb)'
            )
              and position('commercial_ally_runtime_bindings' in definition) > 0
              and position('b.status = ''active''' in definition) > 0
              and position('for update' in lower(definition)) > 0
        )::int
        + (
            to_regprocedure(
                'public.admit_portable_observed_lead_precheckout(text,text,integer,text,jsonb,jsonb)'
            ) is not null
            and not exists(
                select 1
                from functions function_row
                cross join lateral aclexplode(coalesce(
                    function_row.proacl,
                    acldefault('f', function_row.proowner)
                )) acl
                where function_row.oid = to_regprocedure(
                    'public.admit_portable_observed_lead_precheckout(text,text,integer,text,jsonb,jsonb)'
                )
                  and acl.grantee = 0
                  and acl.privilege_type = 'EXECUTE'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_portable_observed_lead_precheckout(text,text,integer,text,jsonb,jsonb)'
                ),
                'EXECUTE'
            )
        )::int,
        3,
        'portable_precheckout_binding_fenced_admission'
    union all
    select
        '20260901000300',
        '20260901000300_commercial_ally_portable_purchase_stop.sql',
        (to_regclass(
            'public.commercial_ally_hotmart_purchase_policies'
        ) is not null)::int
        + (to_regclass(
            'public.portable_hotmart_purchase_correlations'
        ) is not null)::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)'
            )
              and prosecdef
              and proconfig @> array['search_path=pg_catalog, public, pg_temp']
              and position('commercial_ally_runtime_bindings' in definition) > 0
              and position('for update' in lower(definition)) > 0
        )::int
        + (
            to_regprocedure(
                'public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)'
            ) is not null
            and not exists(
                select 1
                from functions function_row
                cross join lateral aclexplode(coalesce(
                    function_row.proacl,
                    acldefault('f', function_row.proowner)
                )) acl
                where function_row.oid = to_regprocedure(
                    'public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)'
                )
                  and acl.grantee = 0
                  and acl.privilege_type = 'EXECUTE'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)'
                ),
                'EXECUTE'
            )
        )::int,
        4,
        'portable_purchase_stop_binding_fenced_default_off'
    union all
    select
        '20260901000400',
        '20260901000400_commercial_ally_discount_policies.sql',
        (to_regclass(
            'public.commercial_ally_discount_policy_versions'
        ) is not null)::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.resolve_commercial_ally_discount_policy(text,text,integer,text)'
            )
              and prosecdef
              and proconfig @> array['search_path=""']
              and position('commercial_ally_runtime_bindings' in definition) > 0
              and position('statement_timestamp()' in definition) > 0
        )::int
        + exists(
            select 1
            from pg_trigger
            where tgrelid = to_regclass(
                'public.commercial_ally_discount_policy_versions'
            )
              and tgname = 'commercial_ally_discount_policy_guard'
              and not tgisinternal
        )::int
        + (
            to_regprocedure(
                'public.resolve_commercial_ally_discount_policy(text,text,integer,text)'
            ) is not null
            and not has_function_privilege(
                'public',
                to_regprocedure(
                    'public.resolve_commercial_ally_discount_policy(text,text,integer,text)'
                ),
                'EXECUTE'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.resolve_commercial_ally_discount_policy(text,text,integer,text)'
                ),
                'EXECUTE'
            )
        )::int,
        4,
        'versioned_discount_policy_default_off'
    union all
    select
        '20260903000100',
        '20260903000100_commercial_ally_portable_recovery.sql',
        (to_regclass(
            'public.commercial_ally_hotmart_event_bindings'
        ) is not null)::int
        + (to_regprocedure(
            'public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)'
        ) is not null)::int
        + exists(
            select 1 from pg_constraint
            where conrelid = to_regclass(
                'public.commercial_ally_hotmart_event_bindings'
            )
              and conname = 'commercial_ally_hotmart_event_bindings_scope_fk'
              and contype = 'f'
              and confrelid = to_regclass(
                  'public.hotmart_purchase_intent_scopes'
              )
              and confdeltype = 'r'
        )::int
        + exists(
            select 1 from pg_trigger
            where tgrelid = to_regclass(
                'public.commercial_ally_hotmart_event_bindings'
            )
              and tgname = 'commercial_ally_hotmart_event_bindings_append_only'
              and not tgisinternal
        )::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.protect_commercial_ally_hotmart_event_binding()'
            )
              and prosecdef
              and not has_function_privilege('public', oid, 'EXECUTE')
              and not has_function_privilege('service_role', oid, 'EXECUTE')
        )::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)'
            )
              and prosecdef
              and proconfig @> array['search_path=pg_catalog, public, pg_temp']
              and position('commercial_ally_runtime_bindings' in definition) > 0
              and position('hotmart_purchase_intent_scopes' in definition) > 0
              and position('commercial_ally_hotmart_event_bindings' in definition) > 0
              and position('portable_hotmart_cart_replay_binding_mismatch' in definition) > 0
        )::int
        + (
            not has_function_privilege(
                'public',
                to_regprocedure(
                    'public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)'
                ),
                'EXECUTE'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)'
                ),
                'EXECUTE'
            )
        )::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.admit_johanna_hotmart_cart_abandonment(text,jsonb,text,text)'
            )
              and prosecdef
              and proconfig @> array['search_path=pg_catalog, public, pg_temp']
              and position('johanna_hotmart_cart_scope_mismatch' in definition) > 0
              and position('8104005' in definition) > 0
              and position('bxjge6zq' in definition) > 0
        )::int
        + (
            has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_johanna_hotmart_cart_abandonment(text,jsonb,text,text)'
                ),
                'EXECUTE'
            )
            and not has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)'
                ),
                'EXECUTE'
            )
        )::int,
        9,
        'portable_cart_recovery_binding_fenced'
    union all
    select
        '20260903000200',
        '20260903000200_commercial_ally_indefinite_discount.sql',
        (
            select count(*)::int
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'commercial_ally_discount_policy_versions'
              and column_name in (
                  'offer_expiration_mode',
                  'requires_inbound_reply_after_initial_template',
                  'coupon_delivery_mode',
                  'release_requires_exact_trigger_set'
              )
        )
        + exists(
            select 1 from pg_trigger
            where tgrelid = to_regclass(
                'public.commercial_ally_discount_policy_versions'
            )
              and tgname = 'commercial_ally_discount_release_complete'
              and tgdeferrable
              and tginitdeferred
              and not tgisinternal
        )::int
        + exists(
            select 1 from functions
            where oid = to_regprocedure(
                'public.resolve_commercial_ally_discount_policy(text,text,integer,text)'
            )
              and prosecdef
              and position('offer_valid_for_seconds' in definition) > 0
              and position('coupon_delivery_mode' in definition) > 0
        )::int
        + (
            not has_function_privilege(
                'public',
                to_regprocedure(
                    'public.resolve_commercial_ally_discount_policy(text,text,integer,text)'
                ),
                'EXECUTE'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.resolve_commercial_ally_discount_policy(text,text,integer,text)'
                ),
                'EXECUTE'
            )
        )::int,
        7,
        'indefinite_atomic_discount_release'
    union all
    select
        '20260903000300',
        '20260903000300_commercial_ally_payment_failure_recovery.sql',
        (to_regclass('public.commercial_ally_payment_failure_details') is not null)::int
        + (to_regclass('public.commercial_ally_payment_failure_conflicts') is not null)::int
        + (to_regprocedure(
            'public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)'
        ) is not null)::int
        + (to_regprocedure(
            'public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)'
        ) is not null)::int
        + (to_regprocedure(
            'public.mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamptz)'
        ) is not null)::int
        + exists(
            select 1 from pg_constraint
            where conrelid = to_regclass('public.recovery_case_events')
              and conname = 'recovery_case_events_event_role_check'
              and position('payment_failure' in pg_get_constraintdef(oid)) > 0
        )::int
        + (
            not has_function_privilege(
                'public',
                to_regprocedure(
                    'public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)'
                ),
                'EXECUTE'
            )
            and has_function_privilege(
                'service_role',
                to_regprocedure(
                    'public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)'
                ),
                'EXECUTE'
            )
        )::int,
        7,
        'portable_payment_failure_recovery_fenced'
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
