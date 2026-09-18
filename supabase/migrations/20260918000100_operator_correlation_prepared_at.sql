-- Keep the exact ten-minute command lifetime without two independent clocks.
-- Preserve the existing scope, snapshots, idempotency, permissions and trigger.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $operator_correlation_prepared_at$
declare
    v_function regprocedure := to_regprocedure(
        'public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)'
    );
    v_trigger regprocedure := to_regprocedure(
        'public.validate_operator_correlation_resolution_command_insert()'
    );
    v_definition text;
    v_trigger_definition text;
    v_owner oid;
    v_acl aclitem[];
    v_old_fragments text[] := array[
        $old$    v_command public.operator_correlation_resolution_commands%rowtype;$old$,
        $old$    insert into public.operator_correlation_resolution_commands ($old$,
        $old$        candidate_snapshot,
        expires_at
$old$,
        $old$        v_candidate_snapshot,
        clock_timestamp() + interval '10 minutes'
$old$
    ];
    v_new_fragments text[] := array[
        $new$    v_command public.operator_correlation_resolution_commands%rowtype;
    v_prepared_at timestamptz;$new$,
        $new$    v_prepared_at := clock_timestamp();
    insert into public.operator_correlation_resolution_commands ($new$,
        $new$        candidate_snapshot,
        prepared_at,
        expires_at
$new$,
        $new$        v_candidate_snapshot,
        v_prepared_at,
        v_prepared_at + interval '10 minutes'
$new$
    ];
    v_index integer;
    v_occurrences integer;
begin
    if v_function is null or v_trigger is null then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_prepared_at_function_missing';
    end if;

    select pg_get_functiondef(oid), proowner, proacl
      into strict v_definition, v_owner, v_acl
      from pg_catalog.pg_proc
      where oid = v_function;
    v_trigger_definition := pg_get_functiondef(v_trigger);
    if position(
        'new.expires_at is distinct from new.prepared_at + interval ''10 minutes'''
        in v_trigger_definition
    ) = 0 then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_prepared_at_guard_missing';
    end if;

    for v_index in array_lower(v_old_fragments, 1)..array_upper(v_old_fragments, 1) loop
        v_occurrences := (
            length(v_definition)
            - length(replace(v_definition, v_old_fragments[v_index], ''))
        ) / length(v_old_fragments[v_index]);
        if v_occurrences <> 1 then
            raise exception using
                errcode = '55000',
                message = 'operator_correlation_prepared_at_definition_mismatch';
        end if;
        v_definition := replace(
            v_definition, v_old_fragments[v_index], v_new_fragments[v_index]
        );
    end loop;
    execute v_definition;

    if pg_get_functiondef(v_trigger) is distinct from v_trigger_definition
       or not exists (
           select 1 from pg_catalog.pg_proc
           where oid = v_function
             and proowner = v_owner
             and proacl is not distinct from v_acl
             and prosecdef
             and proconfig @> array['search_path=pg_catalog, public, pg_temp']
             and pg_get_functiondef(oid) = v_definition
       ) then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_prepared_at_postflight_failed';
    end if;
end;
$operator_correlation_prepared_at$;

commit;
