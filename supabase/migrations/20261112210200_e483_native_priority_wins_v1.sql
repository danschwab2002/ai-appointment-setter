-- e483-native-priority-wins-v1: `prioridad.le_gana_a` en el motor nativo
-- (E-483, C7 del mapa «el AE maneja todos los flujos», 2026-11-12).
-- Contrato: docs/contracts/flow-engine-v2.md («Un flujo nativo le gana a otro»).
--
-- Por que. El esquema setter-flujo/v1 tiene prioridad.le_gana_a, pero el motor
-- nativo lee solo no_arranca_mientras (20261024000200). La regla de ATT1 desde
-- el 18/10 (D4 del mapa): el pago rechazado le gana a un carrito en curso, lo
-- corta. Con varios toques (20261112210000) un carrito nativo en curso tiene A2
-- y Z esperando: sin esto, la persona que despues pago con una tarjeta
-- rechazada recibe el mensaje del rechazo y, ademas, los del carrito.
--
-- Que cambia:
--   1. _flow_engine_native_priority_wins_v1(caso que arranco, evento, ahora),
--      privada (ningun rol de la API la ejecuta). Lee la version fijada del
--      recorrido que acaba de arrancar y, para cada recorrido nativo en curso
--      (grace_period, active o paused) de la misma persona y el mismo producto
--      de un flujo de su le_gana_a (por su clave, como no_arranca_mientras), en
--      otro caso:
--        - si alguna accion viva del caso ya esta mandandose (con un lease que
--          no vencio, con un intento sin terminar o en delivery_unknown), el
--          caso no se toca (un lease vencido no cuenta);
--        - si no, sus acciones vivas (pending, deferred, retryable_failed)
--          quedan cancelled con terminal_reason le_gana_otro_flujo, sus
--          secuencias cancelled con ese cancel_reason, y el caso cancelled con
--          next_contact_reason le_gana_otro_flujo, la marca closed_by_priority
--          en context y un conversation_events recovery_case_closed_by_priority.
--      En el caso que gano queda un flow_engine_native_priority_won con lo que
--      corto (solo si encontro alguno). Es el camino de
--      _flow_change_cancel_in_flight_v1 (20261112210100), con su orden de
--      candados: contacto, caso, secuencias, acciones. Un recorrido del legado
--      (legacy_kind, la ruta motor del carrito de ATT1) no se mira.
--   2. admit_flow_engine_event_v1: despues de que un flujo arranca un recorrido
--      nuevo (motor_arranco y created), y solo si el bridge lo pide con
--      p_instance.native_priority_wins = true (FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED),
--      llama a la funcion de 1 en la misma subtransaccion del flujo: o arranca
--      y corta, o ninguna de las dos.
--
-- Lo que NO cambia. Sin la clave en p_instance (el bridge de hoy, o con el flag
-- apagado) la admision es exactamente la de antes: la funcion de 1 no se llama.
-- La repeticion de un evento (created false), un flujo que no arranca y el
-- primer contacto (que admite por otra RPC) no cortan nada. No toca tablas ni
-- permisos de lo existente.
--
-- Como: el metodo de 20261112210000, reemplazo contado sobre la definicion
-- VIGENTE, con la marca que no puede estar. Correrla dos veces aborta con 55000.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $guard$
begin
    if to_regprocedure('public._flow_engine_native_priority_wins_v1(uuid,uuid,timestamptz)') is not null then
        raise exception using errcode = '55000', message = 'e483_native_priority_wins_already_applied';
    end if;
    if to_regprocedure('public.admit_flow_engine_event_v1(jsonb,jsonb)') is null then
        raise exception using errcode = '55000', message = 'e483_native_priority_wins_missing';
    end if;
end;
$guard$;

-- ---------------------------------------------------------------------------
-- 1. Cortar los recorridos a los que el flujo que arranco le gana.
-- ---------------------------------------------------------------------------
create function public._flow_engine_native_priority_wins_v1(
    p_case_id uuid,
    p_event_id uuid,
    p_now timestamptz
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- e483_native_priority_wins: el motivo que nombra el contrato del motor.
    c_reason constant text := 'le_gana_otro_flujo';
    v_winner public.flow_engine_runs%rowtype;
    v_beats jsonb;
    v_run record;
    v_case public.recovery_cases%rowtype;
    v_live integer;
    v_actions uuid[];
    v_sequences uuid[];
    v_closed integer := 0;
    v_sending integer := 0;
    v_cut jsonb := '[]'::jsonb;
begin
    if p_case_id is null or p_now is null then
        raise exception using errcode = '22023', message = 'priority_wins_arguments_required';
    end if;
    select run.* into v_winner
      from public.flow_engine_runs run
     where run.recovery_case_id = p_case_id and run.legacy_kind is null;
    if not found then
        return jsonb_build_object('cerrados', 0, 'mandandose', 0);
    end if;
    select definition_row.definition #> '{prioridad,le_gana_a}' into v_beats
      from public.flow_definitions definition_row
     where definition_row.flow_id = v_winner.flow_id and definition_row.version = v_winner.version;
    if jsonb_typeof(v_beats) is distinct from 'array' or jsonb_array_length(v_beats) = 0 then
        return jsonb_build_object('cerrados', 0, 'mandandose', 0);
    end if;

    for v_run in
        select run.recovery_case_id, run.flow_id, run.flow_key, run.version
          from public.flow_engine_runs run
          join public.recovery_cases case_row on case_row.id = run.recovery_case_id
         where run.person_ref = v_winner.person_ref
           and run.product_ref = v_winner.product_ref
           and run.legacy_kind is null
           and run.flow_id <> v_winner.flow_id
           and run.recovery_case_id <> p_case_id
           and run.flow_key in (select value from jsonb_array_elements_text(
                   (select coalesce(jsonb_agg(item), '[]'::jsonb) from jsonb_array_elements(v_beats) item
                     where jsonb_typeof(item) = 'string')))
           and case_row.status in ('grace_period', 'active', 'paused')
         order by run.recovery_case_id
    loop
        -- El orden global de candados (claim_due_followup_actions): contacto,
        -- caso, secuencias, acciones. El contacto ya lo tiene la admision.
        perform 1 from public.contacts contact_row where contact_row.id = v_winner.person_ref for update;
        select case_row.* into v_case
          from public.recovery_cases case_row
         where case_row.id = v_run.recovery_case_id
         for update;
        if v_case.status not in ('grace_period', 'active', 'paused') then
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
                    or (action_row.lease_owner is not null
                        and action_row.lease_expires_at > p_now)
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
                   'closed_by_priority', jsonb_build_object(
                       'from_status', v_case.status,
                       'from_next_contact_at', v_case.next_contact_at,
                       'from_next_contact_reason', v_case.next_contact_reason,
                       'flow_id', v_run.flow_id,
                       'flow_key', v_run.flow_key,
                       'version', v_run.version,
                       'winner_flow_id', v_winner.flow_id,
                       'winner_flow_key', v_winner.flow_key,
                       'winner_case_id', p_case_id,
                       'event_id', p_event_id,
                       'action_ids', to_jsonb(v_actions),
                       'sequence_ids', to_jsonb(v_sequences),
                       'closed_at', p_now
                   )
               )
         where case_row.id = v_case.id;

        insert into public.conversation_events (recovery_case_id, event_type, actor_type, data)
        values (v_case.id, 'recovery_case_closed_by_priority', 'system',
            jsonb_build_object('reason_code', c_reason, 'from_status', v_case.status,
                'flow_id', v_run.flow_id, 'flow_key', v_run.flow_key,
                'winner_flow_id', v_winner.flow_id, 'winner_flow_key', v_winner.flow_key,
                'winner_case_id', p_case_id, 'event_id', p_event_id,
                'cancelled_actions', cardinality(v_actions)));
        v_closed := v_closed + 1;
        v_cut := v_cut || jsonb_build_array(jsonb_build_object('recovery_case_id', v_case.id,
            'flow_key', v_run.flow_key));
    end loop;

    if v_closed + v_sending > 0 then
        insert into public.conversation_events (recovery_case_id, event_type, actor_type, data)
        values (p_case_id, 'flow_engine_native_priority_won', 'system',
            jsonb_build_object('reason_code', c_reason, 'flow_id', v_winner.flow_id,
                'flow_key', v_winner.flow_key, 'event_id', p_event_id,
                'cerrados', v_closed, 'mandandose', v_sending, 'casos', v_cut));
    end if;
    return jsonb_build_object('cerrados', v_closed, 'mandandose', v_sending);
end;
$function$;

do $acl$
declare
    v_signature constant text := 'public._flow_engine_native_priority_wins_v1(uuid,uuid,timestamptz)';
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
        raise exception using errcode = '55000', message = 'e483_native_priority_wins_acl_unexpected',
            detail = format('ningun rol de la API ejecuta %s', v_signature);
    end if;
end;
$acl$;

-- ---------------------------------------------------------------------------
-- 2. El reemplazo contado en la admision.
-- ---------------------------------------------------------------------------
do $e483$
declare
    c_admit constant text := 'public.admit_flow_engine_event_v1(jsonb,jsonb)';
    c_marker constant text := 'e483_native_priority_wins';
    c_old constant text :=
        E'                  v_destination) one;\n'
        || E'        exception when others then\n';
    c_new constant text :=
        E'                  v_destination) one;\n'
        || E'            -- e483_native_priority_wins: begin (20261112210200)\n'
        || E'            -- Un recorrido nuevo corta los de su le_gana_a, solo si el bridge lo pide\n'
        || E'            -- (FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED); en la misma subtransaccion.\n'
        || E'            if p_instance -> ''native_priority_wins'' = ''true''::jsonb\n'
        || E'               and v_one.outcome = ''motor_arranco'' and v_one.created is true then\n'
        || E'                perform public._flow_engine_native_priority_wins_v1(\n'
        || E'                    v_one.recovery_case_id, v_event.id, clock_timestamp());\n'
        || E'            end if;\n'
        || E'            -- e483_native_priority_wins: end\n'
        || E'        exception when others then\n';
    v_text text;
    v_acl text;
    v_secdef boolean;
begin
    v_text := pg_get_functiondef(c_admit::regprocedure);
    if position(c_marker in v_text) > 0 then
        raise exception using errcode = '55000', message = 'e483_native_priority_wins_already_applied';
    end if;
    if length(v_text) - length(replace(v_text, c_old, '')) <> length(c_old) then
        raise exception using errcode = '55000', message = 'unexpected_admit_flow_engine_event_definition',
            detail = c_admit;
    end if;
    select coalesce(p.proacl::text, ''), p.prosecdef into v_acl, v_secdef
      from pg_proc p where p.oid = c_admit::regprocedure;
    execute replace(v_text, c_old, c_new);
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_admit::regprocedure) is distinct from v_acl
       or (select p.prosecdef from pg_proc p where p.oid = c_admit::regprocedure) is distinct from v_secdef then
        raise exception using errcode = '55000', message = 'e483_native_priority_wins_acl_changed', detail = c_admit;
    end if;
    if position('e483_native_priority_wins: end' in pg_get_functiondef(c_admit::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_native_priority_wins_not_applied';
    end if;
end;
$e483$;

commit;
