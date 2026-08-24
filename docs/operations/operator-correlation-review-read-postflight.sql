-- Postflight read-only para la proyección de correlaciones pendientes.
-- Estado: procedimiento; no constituye evidencia de ejecución en Cloud.
-- No devuelve PII ni payloads.

begin transaction read only;

select
    p.oid::regprocedure::text as signature,
    p.prosecdef as security_definer,
    array_to_string(p.proconfig, ',') as function_config,
    has_function_privilege('anon', p.oid, 'execute') as anon_execute,
    has_function_privilege('authenticated', p.oid, 'execute') as authenticated_execute,
    has_function_privilege('service_role', p.oid, 'execute') as service_role_execute
from pg_proc p
where p.oid in (
    'public.list_operator_unresolved_correlations(text,text,integer,uuid)'::regprocedure,
    'public.get_operator_unresolved_correlation(text,text,uuid)'::regprocedure
)
order by signature;

select
    has_table_privilege(
        'service_role',
        'public.purchase_intents',
        'select'
    ) as service_role_purchase_intents_select,
    has_table_privilege(
        'service_role',
        'public.hotmart_purchase_intent_correlations',
        'select'
    ) as service_role_correlations_select;

select
    count(*)::integer as scoped_pending_count,
    coalesce(bool_and(
        case_data ->> 'outcome' in ('unmatched', 'ambiguous', 'conflict')
        and (case_data ->> 'manual_handoff_required')::boolean
        and not ((case_data -> 'identity') ? 'normalized_email')
        and not ((case_data -> 'identity') ? 'normalized_phone')
    ), true) as projection_contains_only_unresolved
from public.list_operator_unresolved_correlations(
    'lancemos',
    'psicologajohanna',
    50,
    null
);

rollback;
