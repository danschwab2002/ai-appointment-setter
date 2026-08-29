-- Permit a supervised operator resolution to authorize the existing V2
-- Johanna abandonment one-shot without rewriting deterministic correlation
-- evidence or relaxing any recipient, consent, scope, opt-out, or budget gate.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $operator_resolution_one_shot$
declare
    v_oid oid := to_regprocedure(
        'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)'
    );
    v_definition text;
    v_old_declaration text := E'    blocked_owner_count integer;\n';
    v_new_declaration text := E'    blocked_owner_count integer;\n    operator_resolution_authorized boolean;\n';
    v_old_control text := E'    if control.generation is distinct from p_expected_generation then\n        raise exception using errcode = ''40001'', message = ''johanna_abandonment_hotmart_auto_generation_mismatch'';\n    end if;\n\n';
    v_new_control text := E'    if control.generation is distinct from p_expected_generation then\n        raise exception using errcode = ''40001'', message = ''johanna_abandonment_hotmart_auto_generation_mismatch'';\n    end if;\n\n    select exists (\n        select 1\n        from public.operator_correlation_resolutions resolution\n        where resolution.webhook_event_id = p_hotmart_webhook_event_id\n          and resolution.resolution_outcome = ''linked_candidate''\n          and resolution.effective_purchase_intent_id = p_purchase_intent_id\n          and resolution.deterministic_outcome = ''conflict''\n          and resolution.verification_basis = ''operator_source_record''\n    ) into operator_resolution_authorized;\n\n';
    v_old_correlation text := E'      and correlation.outcome = ''resolved''\n      and correlation.purchase_intent_id = p_purchase_intent_id\n      and correlation.candidate_count = 1\n      and not correlation.manual_handoff_required\n';
    v_new_correlation text := E'      and (\n          (\n              correlation.outcome = ''resolved''\n              and correlation.purchase_intent_id = p_purchase_intent_id\n              and correlation.candidate_count = 1\n              and not correlation.manual_handoff_required\n          )\n          or (\n              operator_resolution_authorized\n              and correlation.outcome = ''conflict''\n              and correlation.reason_code = ''email_phone_conflict''\n              and correlation.purchase_intent_id is null\n              and correlation.candidate_count = 1\n              and correlation.manual_handoff_required\n          )\n      )\n';
    v_old_activation text := E'       or not intent.activation_authorized\n';
    v_new_activation text := E'       or (not intent.activation_authorized and not operator_resolution_authorized)\n';
    v_old_classification text := E'       or intent.current_classification <> ''confirmed_abandonment''\n';
    v_new_classification text := E'       or (\n           intent.current_classification <> ''confirmed_abandonment''\n           and not (\n               operator_resolution_authorized\n               and intent.current_classification = ''identity_conflict''\n           )\n       )\n';
begin
    if v_oid is null then
        raise exception using errcode = '55000',
            message = 'johanna_operator_resolution_one_shot_function_missing';
    end if;
    select pg_get_functiondef(v_oid) into v_definition;
    if (length(v_definition) - length(replace(v_definition, v_old_declaration, '')))
           / length(v_old_declaration) <> 1
       or (length(v_definition) - length(replace(v_definition, v_old_control, '')))
           / length(v_old_control) <> 1
       or (length(v_definition) - length(replace(v_definition, v_old_correlation, '')))
           / length(v_old_correlation) <> 1
       or (length(v_definition) - length(replace(v_definition, v_old_activation, '')))
           / length(v_old_activation) <> 1
       or (length(v_definition) - length(replace(v_definition, v_old_classification, '')))
           / length(v_old_classification) <> 1 then
        raise exception using errcode = '55000',
            message = 'johanna_operator_resolution_one_shot_definition_mismatch';
    end if;
    v_definition := replace(v_definition, v_old_declaration, v_new_declaration);
    v_definition := replace(v_definition, v_old_control, v_new_control);
    v_definition := replace(v_definition, v_old_correlation, v_new_correlation);
    v_definition := replace(v_definition, v_old_activation, v_new_activation);
    v_definition := replace(v_definition, v_old_classification, v_new_classification);
    execute v_definition;
end;
$operator_resolution_one_shot$;

do $operator_resolution_one_shot_postflight$
declare
    v_oid oid := to_regprocedure(
        'public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)'
    );
    v_definition text;
    v_owner text;
    v_security_definer boolean;
    v_search_path_ok boolean;
begin
    select pg_get_functiondef(p.oid), pg_get_userbyid(p.proowner), p.prosecdef,
           p.proconfig @> array['search_path=pg_catalog, public, pg_temp']
    into v_definition, v_owner, v_security_definer, v_search_path_ok
    from pg_proc p where p.oid = v_oid;
    if v_owner <> 'postgres'
       or not v_security_definer
       or not v_search_path_ok
       or (length(v_definition) - length(replace(
           v_definition, 'operator_resolution_authorized', ''
       ))) / length('operator_resolution_authorized') <> 5
       or position('resolution.resolution_outcome = ''linked_candidate''' in v_definition) = 0
       or position('correlation.reason_code = ''email_phone_conflict''' in v_definition) = 0
       or position('intent.current_classification = ''identity_conflict''' in v_definition) = 0
       or position('not intent.whatsapp_contact_authorized' in v_definition) = 0
       or position('from public.contact_opt_out_events stop' in v_definition) = 0 then
        raise exception using errcode = '55000',
            message = 'johanna_operator_resolution_one_shot_postflight_failed';
    end if;
end;
$operator_resolution_one_shot_postflight$;

commit;
