-- e483-native-cancel-in-flight-v1: `en_curso: cancelar` para la version y el
-- volver de un flujo nativo (E-483, C8 del mapa «el AE maneja todos los
-- flujos», 2026-11-12).
-- Contrato: docs/contracts/flow-change-cycle-v1.md («Lo que esta en curso
-- cuando cambia la version»).
--
-- Por que. Con varios toques (20261112210000), una version con un error deja
-- gente a mitad de la secuencia: A1 ya salio y A2 espera con la plantilla de
-- la version vieja. Hasta aca una propuesta aceptaba solo `en_curso: seguir`
-- (_flow_change_items_are_valid rechazaba cualquier otro valor) y el validador
-- del bridge daba en_curso_not_supported.
--
-- Que cambia:
--   1. _flow_change_cancel_in_flight_v1(flujo, version nueva, propuesta, ahora),
--      privada (ningun rol de la API la ejecuta). Para cada recorrido nativo
--      del flujo (flow_engine_runs sin legacy_kind) atado a otra version que la
--      que queda activa, con el caso abierto (grace_period o active):
--        - si alguna accion viva del caso ya esta mandandose (con lease, con un
--          intento sin terminar o en delivery_unknown), el caso no se toca;
--        - si no, sus acciones vivas (pending, deferred, retryable_failed)
--          quedan cancelled con terminal_reason cambio_de_version, sus
--          secuencias cancelled con cancel_reason cambio_de_version, y el caso
--          cancelled (como el cierre por el cupon de una campana: no se gana y
--          no se reintenta) con next_contact_reason cambio_de_version, la marca
--          closed_by_version_change en context (para volver atras caso por
--          caso) y un conversation_events recovery_case_closed_by_version_change.
--      Los candados, en el orden global de claim_due_followup_actions:
--      contacto, caso, secuencias, acciones. Devuelve cuantos cerro y cuantos
--      dejo por tener un envio en curso.
--   2. _flow_change_items_are_valid: en una version o un volver, en_curso vale
--      seguir o cancelar (migrar sigue sin existir).
--   3. _flow_change_publish:
--        - antes de escribir: un cancelar sobre un flujo que no es nativo (el
--          legado, con o sin la ruta motor) no publica, con el motivo
--          en_curso_not_supported y su evento flow_change_publish_rejected;
--        - al escribir: despues de mover el control a la version nueva, un
--          cancelar llama a la funcion de 1; el resumen del evento
--          flow_change_published lleva lo que cancelo (cancelados), solo con
--          cancelar: con seguir el resumen es el de antes.
--
-- Lo que NO cambia. Un `seguir` publica exactamente como antes (la funcion de
-- 1 no se llama). Los recorridos nuevos arrancan con la version activa, como
-- hoy. Nadie podia mandar `cancelar` hasta aca (la base y el validador lo
-- rechazaban), asi que nada de lo que corre en ATT1 o Maribel cambia al
-- aplicarla. No toca tablas ni permisos de lo existente.
--
-- Como: el metodo de 20261112210000, reemplazo contado sobre la definicion
-- VIGENTE de cada funcion, con la marca que no puede estar. Correrla dos veces
-- aborta con 55000.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $guard$
begin
    if to_regprocedure('public._flow_change_cancel_in_flight_v1(uuid,integer,uuid,timestamptz)') is not null then
        raise exception using errcode = '55000', message = 'e483_native_cancel_in_flight_already_applied';
    end if;
    if to_regprocedure('public._flow_change_items_are_valid(jsonb)') is null
       or to_regprocedure('public._flow_change_publish(text,text,uuid,text,jsonb)') is null then
        raise exception using errcode = '55000', message = 'e483_native_cancel_in_flight_missing';
    end if;
end;
$guard$;

-- ---------------------------------------------------------------------------
-- 1. Cancelar lo que espera de un flujo nativo.
-- ---------------------------------------------------------------------------
create function public._flow_change_cancel_in_flight_v1(
    p_flow_id uuid,
    p_new_version integer,
    p_proposal_id uuid,
    p_now timestamptz
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- e483_native_cancel_in_flight: el motivo que nombra el contrato del ciclo.
    c_reason constant text := 'cambio_de_version';
    v_run record;
    v_case public.recovery_cases%rowtype;
    v_live integer;
    v_actions uuid[];
    v_sequences uuid[];
    v_closed integer := 0;
    v_sending integer := 0;
begin
    if p_flow_id is null or p_new_version is null or p_now is null then
        raise exception using errcode = '22023', message = 'cancel_in_flight_arguments_required';
    end if;
    for v_run in
        select run.recovery_case_id, run.version, case_row.contact_id
          from public.flow_engine_runs run
          join public.recovery_cases case_row on case_row.id = run.recovery_case_id
         where run.flow_id = p_flow_id
           and run.legacy_kind is null
           and run.version <> p_new_version
           and case_row.status in ('grace_period', 'active')
         order by run.recovery_case_id
    loop
        -- El orden global de candados (claim_due_followup_actions): contacto,
        -- caso, secuencias, acciones.
        perform 1 from public.contacts contact_row where contact_row.id = v_run.contact_id for update;
        select case_row.* into v_case
          from public.recovery_cases case_row
         where case_row.id = v_run.recovery_case_id
         for update;
        if v_case.status not in ('grace_period', 'active') then
            continue;
        end if;
        perform 1 from public.followup_sequences sequence_row
         where sequence_row.recovery_case_id = v_case.id
         order by sequence_row.id
           for update;
        perform 1 from public.scheduled_actions action_row
         where action_row.recovery_case_id = v_case.id
         order by action_row.id
           for update;

        select count(*) into v_live
          from public.scheduled_actions action_row
         where action_row.recovery_case_id = v_case.id
           and action_row.status in ('pending', 'deferred', 'retryable_failed', 'delivery_unknown');
        if v_live = 0 then
            continue;
        end if;
        -- Lo que ya esta mandandose no se toca: ni la accion ni el caso.
        if exists (
            select 1
              from public.scheduled_actions action_row
             where action_row.recovery_case_id = v_case.id
               and action_row.status in ('pending', 'deferred', 'retryable_failed', 'delivery_unknown')
               and (action_row.status = 'delivery_unknown'
                    or action_row.lease_owner is not null
                    or exists (select 1 from public.followup_delivery_attempts attempt
                                where attempt.action_id = action_row.id
                                  and attempt.phase <> 'completed'))
        ) then
            v_sending := v_sending + 1;
            continue;
        end if;

        with cancelled as (
            update public.scheduled_actions action_row
               set status = 'cancelled',
                   terminal_reason = c_reason,
                   next_attempt_at = null
             where action_row.recovery_case_id = v_case.id
               and action_row.status in ('pending', 'deferred', 'retryable_failed')
            returning action_row.id
        )
        select coalesce(array_agg(id order by id), array[]::uuid[]) into v_actions from cancelled;

        with cancelled as (
            update public.followup_sequences sequence_row
               set status = 'cancelled',
                   cancelled_at = p_now,
                   cancel_reason = c_reason,
                   revision = sequence_row.revision + 1
             where sequence_row.recovery_case_id = v_case.id
               and sequence_row.status in ('active', 'paused')
            returning sequence_row.id
        )
        select coalesce(array_agg(id order by id), array[]::uuid[]) into v_sequences from cancelled;

        update public.recovery_cases case_row
           set status = 'cancelled',
               closed_at = p_now,
               next_contact_at = null,
               next_contact_reason = c_reason,
               version = case_row.version + 1,
               context = case_row.context || jsonb_build_object(
                   'closed_by_version_change', jsonb_build_object(
                       'from_status', v_case.status,
                       'from_next_contact_at', v_case.next_contact_at,
                       'from_next_contact_reason', v_case.next_contact_reason,
                       'flow_id', p_flow_id,
                       'from_version', v_run.version,
                       'to_version', p_new_version,
                       'proposal_id', p_proposal_id,
                       'action_ids', to_jsonb(v_actions),
                       'sequence_ids', to_jsonb(v_sequences),
                       'closed_at', p_now
                   )
               )
         where case_row.id = v_case.id;

        insert into public.conversation_events (recovery_case_id, event_type, actor_type, data)
        values (v_case.id, 'recovery_case_closed_by_version_change', 'system',
            jsonb_build_object('reason_code', c_reason, 'from_status', v_case.status, 'flow_id', p_flow_id,
                'from_version', v_run.version, 'to_version', p_new_version, 'proposal_id', p_proposal_id,
                'cancelled_actions', cardinality(v_actions)));
        v_closed := v_closed + 1;
    end loop;
    return jsonb_build_object('cerrados', v_closed, 'mandandose', v_sending);
end;
$function$;

do $acl$
declare
    v_signature constant text := 'public._flow_change_cancel_in_flight_v1(uuid,integer,uuid,timestamptz)';
    v_role text;
begin
    execute format('revoke all on function %s from public', v_signature);
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function %s from %I', v_signature, v_role);
        end if;
    end loop;
    if exists (
        select 1 from pg_roles
         where rolname in ('anon', 'authenticated', 'service_role')
           and has_function_privilege(rolname, v_signature, 'EXECUTE')
    ) then
        raise exception using errcode = '55000', message = 'e483_native_cancel_in_flight_acl_unexpected',
            detail = format('ningun rol de la API ejecuta %s', v_signature);
    end if;
end;
$acl$;

-- ---------------------------------------------------------------------------
-- 2 y 3. Los reemplazos contados.
-- ---------------------------------------------------------------------------
do $e483$
declare
    c_items constant text := 'public._flow_change_items_are_valid(jsonb)';
    c_publish constant text := 'public._flow_change_publish(text,text,uuid,text,jsonb)';
    c_marker constant text := 'e483_native_cancel_in_flight';
    -- 2. en_curso: seguir o cancelar, en la version y en el volver.
    c_old_comment constant text :=
        E'            -- en_curso vale solo seguir hasta que el motor pueda aplicar cancelar.\n';
    c_new_comment constant text :=
        E'            -- e483_native_cancel_in_flight: en_curso vale seguir o cancelar (20261112210100);\n'
        || E'            -- cancelar solo publica en un flujo nativo (_flow_change_publish).\n';
    c_old_policy constant text := E'and v_item -> ''en_curso'' = ''"seguir"''::jsonb, false) then\n';
    c_new_policy constant text :=
        E'and v_item -> ''en_curso'' in (''"seguir"''::jsonb, ''"cancelar"''::jsonb), false) then\n';
    -- 3a. La variable de lo que cancelo.
    c_old_declare constant text := E'    v_open jsonb;\n';
    c_new_declare constant text :=
        E'    v_open jsonb;\n'
        || E'    -- e483_native_cancel_in_flight: lo que cancelo un en_curso: cancelar.\n'
        || E'    v_in_flight jsonb;\n';
    -- 3b. Antes de escribir: cancelar solo en un nativo.
    c_old_checks constant text := E'    -- flow_change_publish_native_version_guard: end\n';
    c_new_checks constant text :=
        E'    -- flow_change_publish_native_version_guard: end\n'
        || E'    -- e483_native_cancel_in_flight: begin (los chequeos, 20261112210100)\n'
        || E'    -- Cancelar lo que espera solo se aplica en un flujo nativo; el legado (con o sin\n'
        || E'    -- la ruta motor) sigue con seguir.\n'
        || E'    for v_new in select item from jsonb_array_elements(v_proposal.items) item\n'
        || E'                 where item ->> ''tipo'' in (''version'', ''volver'')\n'
        || E'                   and item -> ''en_curso'' = ''"cancelar"''::jsonb\n'
        || E'                   and not exists (select 1 from public.flow_engine_routes r\n'
        || E'                                    where r.flow_id = (item ->> ''flow_id'')::uuid and r.legacy_kind is null) loop\n'
        || E'        perform public._flow_change_event(v_proposal, ''flow_change_publish_rejected'',\n'
        || E'            v_proposal.approved_by_kind, v_proposal.approved_by_ref, ''en_curso_not_supported'',\n'
        || E'            jsonb_build_object(''producto'', v_new ->> ''producto'', ''flujo'', v_new ->> ''flujo''));\n'
        || E'        return jsonb_build_object(''ok'', false, ''reason_code'', ''en_curso_not_supported'');\n'
        || E'    end loop;\n'
        || E'    -- e483_native_cancel_in_flight: end (los chequeos)\n';
    -- 3c. Al escribir: despues del control, cancelar lo que espera.
    c_old_summary constant text :=
        E'        v_summary := v_summary || jsonb_build_array(jsonb_build_object(''flow_id'', v_flow_id,\n'
        || E'            ''from_version'', v_control.active_version, ''to_version'', v_new_version,\n'
        || E'            ''from_state'', v_control.state, ''to_state'', v_new_state,\n'
        || E'            ''en_curso'', v_content ->> ''en_curso''));\n';
    c_new_summary constant text :=
        E'        -- e483_native_cancel_in_flight: begin (cancelar lo que espera, 20261112210100)\n'
        || E'        v_in_flight := null;\n'
        || E'        if v_content -> ''en_curso'' = ''"cancelar"''::jsonb then\n'
        || E'            v_in_flight := public._flow_change_cancel_in_flight_v1(v_flow_id, v_new_version,\n'
        || E'                v_proposal.id, clock_timestamp());\n'
        || E'        end if;\n'
        || E'        -- e483_native_cancel_in_flight: end\n'
        || E'        v_summary := v_summary || jsonb_build_array(jsonb_build_object(''flow_id'', v_flow_id,\n'
        || E'            ''from_version'', v_control.active_version, ''to_version'', v_new_version,\n'
        || E'            ''from_state'', v_control.state, ''to_state'', v_new_state,\n'
        || E'            ''en_curso'', v_content ->> ''en_curso'')\n'
        || E'            -- e483_native_cancel_in_flight: con seguir, el resumen de antes.\n'
        || E'            || case when v_in_flight is null then ''{}''::jsonb\n'
        || E'                    else jsonb_build_object(''cancelados'', v_in_flight) end);\n';
    v_text text;
    v_acl text;
begin
    -- 2.
    v_text := pg_get_functiondef(c_items::regprocedure);
    if position(c_marker in v_text) > 0 then
        raise exception using errcode = '55000', message = 'e483_native_cancel_in_flight_already_applied';
    end if;
    if length(v_text) - length(replace(v_text, c_old_comment, '')) <> length(c_old_comment)
       or length(v_text) - length(replace(v_text, c_old_policy, '')) <> 2 * length(c_old_policy) then
        raise exception using errcode = '55000', message = 'unexpected_flow_change_items_are_valid_definition',
            detail = c_items;
    end if;
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = c_items::regprocedure;
    execute replace(replace(v_text, c_old_comment, c_new_comment), c_old_policy, c_new_policy);
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_items::regprocedure) is distinct from v_acl then
        raise exception using errcode = '55000', message = 'e483_native_cancel_in_flight_acl_changed', detail = c_items;
    end if;

    -- 3.
    v_text := pg_get_functiondef(c_publish::regprocedure);
    if position(c_marker in v_text) > 0 then
        raise exception using errcode = '55000', message = 'e483_native_cancel_in_flight_already_applied';
    end if;
    if length(v_text) - length(replace(v_text, c_old_declare, '')) <> length(c_old_declare)
       or length(v_text) - length(replace(v_text, c_old_checks, '')) <> length(c_old_checks)
       or length(v_text) - length(replace(v_text, c_old_summary, '')) <> length(c_old_summary) then
        raise exception using errcode = '55000', message = 'unexpected_flow_change_publish_definition',
            detail = c_publish;
    end if;
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = c_publish::regprocedure;
    execute replace(replace(replace(v_text, c_old_declare, c_new_declare), c_old_checks, c_new_checks),
                    c_old_summary, c_new_summary);
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_publish::regprocedure) is distinct from v_acl then
        raise exception using errcode = '55000', message = 'e483_native_cancel_in_flight_acl_changed', detail = c_publish;
    end if;

    if public._flow_change_items_are_valid(jsonb_build_array(jsonb_build_object(
           'flow_id', '00000000-0000-0000-0000-000000000000', 'producto', 'p', 'flujo', 'carrito',
           'tipo', 'volver', 'version', 1, 'en_curso', 'cancelar'))) is not true
       or public._flow_change_items_are_valid(jsonb_build_array(jsonb_build_object(
           'flow_id', '00000000-0000-0000-0000-000000000000', 'producto', 'p', 'flujo', 'carrito',
           'tipo', 'volver', 'version', 1, 'en_curso', 'migrar'))) is not false
       or position('e483_native_cancel_in_flight: end (los chequeos)' in pg_get_functiondef(c_publish::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_native_cancel_in_flight_not_applied';
    end if;
end;
$e483$;

commit;
