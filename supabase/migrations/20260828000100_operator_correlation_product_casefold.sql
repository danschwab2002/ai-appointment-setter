-- Keep operator correlation review and supervised resolution aligned with the
-- canonical Hotmart correlator: product references are case-insensitive.
-- The guarded rewrite preserves signatures, owners, ACLs, volatility,
-- SECURITY DEFINER, search_path and every non-product scope predicate.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $operator_correlation_product_casefold$
declare
    v_signatures text[] := array[
        'public.validate_operator_correlation_resolution_command_insert()',
        'public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)',
        'public.confirm_operator_correlation_resolution(text,text,text,uuid,text,uuid)',
        'public.list_operator_unresolved_correlations(text,text,integer,uuid)'
    ];
    v_old_fragments text[] := array[
        'intent.product_ref = v_scope.purchase_intent_product_ref',
        'intent.product_ref = v_scope.purchase_intent_product_ref',
        'intent.product_ref = v_scope.purchase_intent_product_ref',
        'intent.product_ref = scope.purchase_intent_product_ref'
    ];
    v_new_fragments text[] := array[
        'lower(intent.product_ref) = lower(v_scope.purchase_intent_product_ref)',
        'lower(intent.product_ref) = lower(v_scope.purchase_intent_product_ref)',
        'lower(intent.product_ref) = lower(v_scope.purchase_intent_product_ref)',
        'lower(intent.product_ref) = lower(scope.purchase_intent_product_ref)'
    ];
    v_expected_occurrences integer[] := array[2, 1, 2, 1];
    v_index integer;
    v_function regprocedure;
    v_definition text;
    v_occurrences integer;
begin
    for v_index in array_lower(v_signatures, 1)..array_upper(v_signatures, 1) loop
        v_function := to_regprocedure(v_signatures[v_index]);
        if v_function is null then
            raise exception using
                errcode = '55000',
                message = 'operator_correlation_casefold_function_missing';
        end if;

        select pg_get_functiondef(v_function)
          into strict v_definition;

        v_occurrences := (
            length(v_definition)
            - length(replace(v_definition, v_old_fragments[v_index], ''))
        ) / length(v_old_fragments[v_index]);

        if v_occurrences <> v_expected_occurrences[v_index] then
            raise exception using
                errcode = '55000',
                message = 'operator_correlation_casefold_definition_mismatch';
        end if;

        execute replace(
            v_definition,
            v_old_fragments[v_index],
            v_new_fragments[v_index]
        );
    end loop;
end;
$operator_correlation_product_casefold$;

do $operator_correlation_product_casefold_postflight$
declare
    v_signatures text[] := array[
        'public.validate_operator_correlation_resolution_command_insert()',
        'public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)',
        'public.confirm_operator_correlation_resolution(text,text,text,uuid,text,uuid)',
        'public.list_operator_unresolved_correlations(text,text,integer,uuid)'
    ];
    v_expected_occurrences integer[] := array[2, 1, 2, 1];
    v_expected_security_definer boolean[] := array[false, true, true, true];
    v_index integer;
    v_function regprocedure;
    v_definition text;
    v_occurrences integer;
    v_security_definer boolean;
    v_search_path_ok boolean;
begin
    for v_index in array_lower(v_signatures, 1)..array_upper(v_signatures, 1) loop
        v_function := to_regprocedure(v_signatures[v_index]);
        select pg_get_functiondef(v_function)
          into strict v_definition;

        v_occurrences := (
            length(v_definition)
            - length(replace(
                v_definition,
                'lower(intent.product_ref) = lower(',
                ''
            ))
        ) / length('lower(intent.product_ref) = lower(');

        select
            function.prosecdef,
            function.proconfig @> array['search_path=pg_catalog, public, pg_temp']
          into strict v_security_definer, v_search_path_ok
          from pg_proc function
          where function.oid = v_function;

        if v_occurrences <> v_expected_occurrences[v_index] then
            raise exception using
                errcode = '55000',
                message = format(
                    'operator_correlation_casefold_occurrence_postflight_failed_%s_%s',
                    v_index,
                    v_occurrences
                );
        end if;
        if v_security_definer is distinct from v_expected_security_definer[v_index]
           or not v_search_path_ok then
            raise exception using
                errcode = '55000',
                message = format(
                    'operator_correlation_casefold_security_postflight_failed_%s',
                    v_index
                );
        end if;
    end loop;
end;
$operator_correlation_product_casefold_postflight$;

commit;
