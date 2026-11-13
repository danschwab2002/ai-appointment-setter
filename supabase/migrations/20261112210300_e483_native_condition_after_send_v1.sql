-- e483-native-condition-after-send-v1: el pago rechazado completo en el motor
-- nativo: B1 por motivo y, a las 24 h, B2 o Z segun la rama (E-483, C3 del mapa
-- «el AE maneja todos los flujos», 2026-11-12; H3/P9).
-- Contrato: docs/contracts/flow-engine-v2.md («El pago rechazado completo»).
--
-- Por que. El legado de ATT1 manda B1 en una de cuatro variantes por motivo
-- (payment_failure_variant.py) y, a las 24 h, B2 si B1 salio sin cupon o Z si
-- salio con uno (payment_failure_followups.py, 20261019000700). En el nativo la
-- cadena de condiciones (20261103000900) elegia una plantilla por rama, un solo
-- envio; y 20261112210000 cerraba cualquier condicion con mas de un envio.
--
-- La forma (sin tocar el esquema). «El toque anterior llevo cupon» no es un dato
-- del esquema (CONDITION_DATA no lo tiene y sumarlo exige flow_definition.py).
-- No hace falta: en el nativo el cupon de B1 es fijo y esta escrito en la rama
-- (boton.cupon), asi que cada rama sabe si su B1 llevo cupon y lleva su
-- seguimiento: la definicion queda
--   [esperar 15m, condicion por motivo -> cada rama: B1, esperar 24h, B2 o Z]
-- La condicion se evalua una sola vez, antes del primer envio; el toque
-- siguiente sale de la rama que eligio el primer envio aceptado.
--
-- Que cambia, sobre las definiciones vivas (cada reemplazo contado, con su
-- marca; si el texto no esta exactamente una vez, aborta con 55000):
--   1. _flow_engine_native_payment_failure_path_shape(jsonb) y
--      _flow_engine_native_payment_failure_paths(jsonb), nuevas y privadas
--      (ningun rol de la API las ejecuta). La primera dice si una rama es un
--      camino que la base ejecuta: el primer envio (la forma de siempre, con o
--      sin boton) y despues, hasta tres envios en total, una o mas esperas que
--      suman al menos un minuto y un envio sin boton; termina en un envio.
--      Devuelve las esperas entre envios en minutos ([] con un solo envio), o
--      null. La segunda da el camino entero de cada rama, en orden.
--   2. _flow_engine_native_payment_failure_branches: cada rama (y el si_no
--      final) puede ser ese camino, todas con las mismas esperas (la politica
--      es una sola: la de cada rama compila igual). "paso" sigue siendo el
--      primer envio de la rama: la eleccion, su registro y las ventas por rama
--      no cambian.
--   3. _flow_engine_native_unsupported: una cadena de ramas de mas de un envio
--      sin la salida contesta escrita es motor_nativo:forma (la regla de
--      20261112210000 para varios envios).
--   4. _flow_engine_compile_policy_core: con la cadena, compila cada rama con
--      su camino entero (native_touch_2 y 3, con su espera desde el primero).
--   5. _flow_engine_sends: con la cadena, los envios del camino del si_no final
--      (uno, como hoy, si la rama es de un envio).
--   6. flow_engine_execution_context_v1: el toque siguiente (ordinal 2 o 3) de
--      un recorrido nativo con la cadena sale del camino de la rama registrada
--      en flow_engine_native_branch_choices para el primer envio ACEPTADO del
--      caso; sin esa eleccion no hay fila (no se adivina una rama). Y
--      template_branches va solo en el primer envio: en el siguiente la base ya
--      eligio y el bridge no vuelve a elegir (un rechazo nuevo entre B1 y B2 no
--      cambia de rama).
--
-- Lo que NO cambia. Una rama de un solo envio da exactamente lo de antes (las
-- mismas ramas, la misma politica, los mismos envios, el mismo contexto): lo
-- fija validate_e483_native_condition_after_send.mjs con la cadena de Maribel
-- publicada hoy. Una plantilla antes de la cadena (elegir despues de un envio)
-- sigue siendo motor_nativo:forma. El legado no pasa por ninguna de estas
-- funciones. El toque siguiente sale en el bridge solo con
-- FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED (apagado por defecto). No toca tablas
-- ni permisos de lo existente.
--
-- Como: el metodo de 20261112210000, reemplazo contado sobre la definicion
-- VIGENTE, con la marca que no puede estar. Correrla dos veces aborta con 55000.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $guard$
begin
    if to_regprocedure('public._flow_engine_native_payment_failure_path_shape(jsonb)') is not null
       or to_regprocedure('public._flow_engine_native_payment_failure_paths(jsonb)') is not null then
        raise exception using errcode = '55000', message = 'e483_native_condition_after_send_already_applied';
    end if;
    if to_regprocedure('public._flow_engine_native_payment_failure_branches(jsonb)') is null
       or to_regprocedure('public._flow_engine_native_payment_failure_send_ok(jsonb)') is null
       or to_regprocedure('public._flow_engine_duration_minutes(jsonb)') is null
       or to_regclass('public.flow_engine_native_branch_choices') is null then
        raise exception using errcode = '55000', message = 'e483_native_condition_after_send_missing';
    end if;
end;
$guard$;

-- ---------------------------------------------------------------------------
-- 1. El camino de una rama.
-- ---------------------------------------------------------------------------

-- Las esperas entre los envios de una rama, en minutos ([] con un solo envio),
-- o null si la rama no es un camino que la base ejecuta.
create function public._flow_engine_native_payment_failure_path_shape(p_steps jsonb)
returns jsonb
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- e483_native_condition_after_send: hasta tres envios por camino (20261112210300).
    c_max_sends constant integer := 3;
    v_step jsonb;
    v_index integer := 0;
    v_sends integer := 0;
    v_waited bigint := 0;
    v_waiting boolean := false;
    v_minutes integer;
    v_shape jsonb := '[]'::jsonb;
begin
    if jsonb_typeof(p_steps) is distinct from 'array' or jsonb_array_length(p_steps) = 0
       or not public._flow_engine_native_payment_failure_send_ok(p_steps -> 0) then
        return null;
    end if;
    v_sends := 1;
    for v_step in select value from jsonb_array_elements(p_steps) loop
        v_index := v_index + 1;
        continue when v_index = 1;
        if jsonb_typeof(v_step) is distinct from 'object' then
            return null;
        end if;
        if v_step ? 'esperar' and v_step - 'esperar' = '{}'::jsonb then
            v_minutes := public._flow_engine_duration_minutes(v_step -> 'esperar');
            if v_minutes is null then
                return null;
            end if;
            v_waited := v_waited + v_minutes;
            v_waiting := true;
            continue;
        end if;
        -- Un envio siguiente: despues de al menos un minuto de espera, sin boton
        -- (el boton solo en el primero, como en 20261112210000).
        if not v_waiting or v_waited < 1 or v_sends >= c_max_sends
           or not public._flow_engine_native_payment_failure_send_ok(v_step)
           or v_step ? 'boton' then
            return null;
        end if;
        v_sends := v_sends + 1;
        v_shape := v_shape || jsonb_build_array(v_waited);
        v_waited := 0;
        v_waiting := false;
    end loop;
    -- Una espera despues del ultimo envio no lleva a nada.
    if v_waiting then
        return null;
    end if;
    return v_shape;
end;
$function$;

-- El camino entero de cada rama de la cadena, en el orden en que se eligen, o
-- null si la definicion no tiene la forma de la cadena.
create function public._flow_engine_native_payment_failure_paths(p_definition jsonb)
returns jsonb
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_node jsonb;
    v_rest jsonb;
    v_paths jsonb := '[]'::jsonb;
    v_level integer := 0;
begin
    if public._flow_engine_native_payment_failure_branches(p_definition) is null then
        return null;
    end if;
    -- La misma recorrida que _flow_engine_native_payment_failure_branches, que ya valido la forma.
    v_node := p_definition -> 'pasos' -> -1;
    loop
        v_level := v_level + 1;
        v_paths := v_paths || jsonb_build_array(v_node -> 'entonces');
        v_rest := v_node -> 'si_no';
        if v_level < 3 and jsonb_array_length(v_rest) = 1 and jsonb_typeof(v_rest -> 0) = 'object'
           and (v_rest -> 0) ? 'condicion' then
            v_node := v_rest -> 0;
            continue;
        end if;
        return v_paths || jsonb_build_array(v_rest);
    end loop;
end;
$function$;

do $acl$
declare
    v_signature text;
    v_role text;
begin
    foreach v_signature in array array[
        'public._flow_engine_native_payment_failure_path_shape(jsonb)',
        'public._flow_engine_native_payment_failure_paths(jsonb)'] loop
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
            raise exception using errcode = '55000', message = 'e483_native_condition_after_send_acl_unexpected',
                detail = format('ningun rol de la API ejecuta %s', v_signature);
        end if;
    end loop;
end;
$acl$;

-- ---------------------------------------------------------------------------
-- 2 a 6. Los reemplazos contados.
-- ---------------------------------------------------------------------------
do $e483$
declare
    c_marker constant text := 'e483_native_condition_after_send';
    c_branches constant text := 'public._flow_engine_native_payment_failure_branches(jsonb)';
    c_unsupported constant text := 'public._flow_engine_native_unsupported(jsonb)';
    c_core constant text := 'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)';
    c_sends constant text := 'public._flow_engine_sends(jsonb)';
    c_context constant text := 'public.flow_engine_execution_context_v1(uuid)';

    -- 2. Las ramas: cada una, un camino; todas con las mismas esperas.
    c_old_declare constant text := E'    v_level integer := 0;\n';
    c_new_declare constant text :=
        E'    v_level integer := 0;\n'
        || E'    -- e483_native_condition_after_send: las esperas del camino de la primera rama (20261112210300).\n'
        || E'    v_shape jsonb;\n';
    c_old_then constant text :=
        E'           or jsonb_typeof(v_node -> ''entonces'') is distinct from ''array''\n'
        || E'           or jsonb_array_length(v_node -> ''entonces'') <> 1\n'
        || E'           or not public._flow_engine_native_payment_failure_send_ok(v_node -> ''entonces'' -> 0) then\n'
        || E'            return null;\n'
        || E'        end if;\n';
    c_new_then constant text :=
        E'           -- e483_native_condition_after_send: la rama es un camino de hasta tres envios,\n'
        || E'           -- con las mismas esperas en todas (20261112210300).\n'
        || E'           or public._flow_engine_native_payment_failure_path_shape(v_node -> ''entonces'') is null\n'
        || E'           or public._flow_engine_native_payment_failure_path_shape(v_node -> ''entonces'')\n'
        || E'              is distinct from coalesce(v_shape, public._flow_engine_native_payment_failure_path_shape(v_node -> ''entonces'')) then\n'
        || E'            return null;\n'
        || E'        end if;\n'
        || E'        v_shape := public._flow_engine_native_payment_failure_path_shape(v_node -> ''entonces'');\n';
    c_old_rest constant text :=
        E'        if jsonb_typeof(v_rest) is distinct from ''array'' or jsonb_array_length(v_rest) <> 1 then\n'
        || E'            return null;\n'
        || E'        end if;\n'
        || E'        if v_level < 3 and jsonb_typeof(v_rest -> 0) = ''object'' and (v_rest -> 0) ? ''condicion'' then\n';
    c_new_rest constant text :=
        E'        if jsonb_typeof(v_rest) is distinct from ''array'' or jsonb_array_length(v_rest) = 0 then\n'
        || E'            return null;\n'
        || E'        end if;\n'
        || E'        if v_level < 3 and jsonb_array_length(v_rest) = 1\n'
        || E'           and jsonb_typeof(v_rest -> 0) = ''object'' and (v_rest -> 0) ? ''condicion'' then\n';
    c_old_last constant text :=
        E'        if not public._flow_engine_native_payment_failure_send_ok(v_rest -> 0) then\n';
    c_new_last constant text :=
        E'        -- e483_native_condition_after_send: el si_no final, un camino con las mismas esperas.\n'
        || E'        if public._flow_engine_native_payment_failure_path_shape(v_rest) is distinct from v_shape then\n';

    -- 3. Lo que abre la base: varios envios por rama, con contesta.
    c_old_unsupported constant text :=
        E'            then case when public._flow_engine_native_payment_failure_branches(p_definition) is null\n'
        || E'                      then ''motor_nativo:forma'' end\n';
    c_new_unsupported constant text :=
        E'            then case when public._flow_engine_native_payment_failure_branches(p_definition) is null\n'
        || E'                      then ''motor_nativo:forma''\n'
        || E'                      -- e483_native_condition_after_send: con mas de un envio por rama, la salida\n'
        || E'                      -- contesta escrita, como cualquier nativo de varios toques (20261112210300).\n'
        || E'                      when jsonb_array_length(public._flow_engine_native_payment_failure_paths(p_definition) -> 0) > 1\n'
        || E'                           and not coalesce(jsonb_typeof(p_definition #> ''{termina,si}'') = ''array''\n'
        || E'                                            and p_definition #> ''{termina,si}'' @> ''["contesta"]''::jsonb, false)\n'
        || E'                      then ''motor_nativo:forma'' end\n';

    -- 4. El compilador: cada rama con su camino entero.
    c_old_core constant text :=
        E'        for v_branch in select value from jsonb_array_elements(v_branches) loop\n'
        || E'            v_row := public._flow_engine_compile_policy_core(\n'
        || E'                jsonb_set(p_definition, ''{pasos}'',\n'
        || E'                          ((p_definition -> ''pasos'') - (jsonb_array_length(p_definition -> ''pasos'') - 1))\n'
        || E'                          || jsonb_build_array(v_branch -> ''paso'')),\n';
    c_new_core constant text :=
        E'        -- e483_native_condition_after_send: cada rama con su camino entero (20261112210300);\n'
        || E'        -- todas tienen las mismas esperas, asi que compilan a la misma politica.\n'
        || E'        for v_branch in select value from jsonb_array_elements(\n'
        || E'                public._flow_engine_native_payment_failure_paths(p_definition)) loop\n'
        || E'            v_row := public._flow_engine_compile_policy_core(\n'
        || E'                jsonb_set(p_definition, ''{pasos}'',\n'
        || E'                          ((p_definition -> ''pasos'') - (jsonb_array_length(p_definition -> ''pasos'') - 1))\n'
        || E'                          || v_branch),\n';

    -- 5. Los envios: los del camino del si_no final.
    c_old_sends constant text :=
        E'    select 1, branch.paso ->> ''plantilla'', branch.paso ->> ''idioma'', branch.paso -> ''variables'',\n'
        || E'           branch.paso -> ''boton''\n'
        || E'      from (select public._flow_engine_native_payment_failure_branches(p_definition) -> -1 -> ''paso'' as paso) branch\n'
        || E'     where branch.paso is not null\n';
    c_new_sends constant text :=
        E'    -- e483_native_condition_after_send: los envios del camino del si_no final (20261112210300).\n'
        || E'    select (row_number() over (order by branch.step_index))::integer, branch.paso ->> ''plantilla'',\n'
        || E'           branch.paso ->> ''idioma'', branch.paso -> ''variables'', branch.paso -> ''boton''\n'
        || E'      from jsonb_array_elements(coalesce(public._flow_engine_native_payment_failure_paths(p_definition) -> -1,\n'
        || E'                                         ''[]''::jsonb)) with ordinality as branch(paso, step_index)\n'
        || E'     where jsonb_typeof(branch.paso) = ''object'' and branch.paso ? ''plantilla''\n';

    -- 6. El contexto del paso: el toque siguiente, de la rama elegida.
    c_old_context_branches constant text :=
        E'           case when exists (select 1 from public.flow_engine_native_runs native\n'
        || E'                              where native.recovery_case_id = action_row.recovery_case_id)\n'
        || E'                then public._flow_engine_native_payment_failure_branches(definition_row.definition) end\n';
    c_new_context_branches constant text :=
        E'           -- e483_native_condition_after_send: solo en el primer envio; en el siguiente la\n'
        || E'           -- base ya eligio la rama (20261112210300).\n'
        || E'           case when position_row.ordinal = 1\n'
        || E'                 and exists (select 1 from public.flow_engine_native_runs native\n'
        || E'                              where native.recovery_case_id = action_row.recovery_case_id)\n'
        || E'                then public._flow_engine_native_payment_failure_branches(definition_row.definition) end\n';
    c_old_context_send constant text :=
        E'      join lateral public._flow_engine_sends(definition_row.definition) send\n'
        || E'        on send.ordinal = position_row.ordinal\n';
    c_new_context_send constant text :=
        E'      -- e483_native_condition_after_send: begin (20261112210300)\n'
        || E'      -- El toque siguiente de un recorrido nativo con la cadena sale del camino de la\n'
        || E'      -- rama registrada para el primer envio aceptado del caso; sin eleccion, no hay\n'
        || E'      -- envio. Todo lo demas, de _flow_engine_sends, como antes.\n'
        || E'      join lateral (\n'
        || E'          select sent.ordinal, sent.plantilla, sent.idioma, sent.variables, sent.boton\n'
        || E'            from public._flow_engine_sends(definition_row.definition) sent\n'
        || E'           where sent.ordinal = position_row.ordinal\n'
        || E'             and (position_row.ordinal = 1\n'
        || E'                  or public._flow_engine_native_payment_failure_branches(definition_row.definition) is null\n'
        || E'                  or not exists (select 1 from public.flow_engine_native_runs native\n'
        || E'                                  where native.recovery_case_id = action_row.recovery_case_id))\n'
        || E'          union all\n'
        || E'          select chosen.ordinal, chosen.paso ->> ''plantilla'', chosen.paso ->> ''idioma'',\n'
        || E'                 chosen.paso -> ''variables'', chosen.paso -> ''boton''\n'
        || E'            from (select (row_number() over (order by path.step_index))::integer as ordinal, path.paso\n'
        || E'                    from jsonb_array_elements(coalesce(\n'
        || E'                             public._flow_engine_native_payment_failure_paths(definition_row.definition)\n'
        || E'                             -> (select choice.branch_index\n'
        || E'                                   from public.flow_engine_native_branch_choices choice\n'
        || E'                                   join public.followup_delivery_attempts attempt\n'
        || E'                                     on attempt.id = choice.delivery_attempt_id\n'
        || E'                                  where choice.recovery_case_id = action_row.recovery_case_id\n'
        || E'                                    and choice.flow_id = run.flow_id and choice.version = run.version\n'
        || E'                                    and (attempt.outcome = ''accepted_by_chatwoot''\n'
        || E'                                         or attempt.reconciliation_resolution = ''accepted_by_chatwoot'')\n'
        || E'                                  order by choice.created_at desc, choice.delivery_attempt_id\n'
        || E'                                  limit 1),\n'
        || E'                             ''[]''::jsonb)) with ordinality as path(paso, step_index)\n'
        || E'                   where jsonb_typeof(path.paso) = ''object'' and path.paso ? ''plantilla'') chosen\n'
        || E'           where position_row.ordinal > 1\n'
        || E'             and chosen.ordinal = position_row.ordinal\n'
        || E'             and public._flow_engine_native_payment_failure_branches(definition_row.definition) is not null\n'
        || E'             and exists (select 1 from public.flow_engine_native_runs native\n'
        || E'                          where native.recovery_case_id = action_row.recovery_case_id)\n'
        || E'      ) send on true\n'
        || E'      -- e483_native_condition_after_send: end\n';

    v_text text;
    v_acl text;
    v_secdef boolean;
    v_function text;
begin
    foreach v_function in array array[c_branches, c_unsupported, c_core, c_sends, c_context] loop
        if to_regprocedure(v_function) is null then
            raise exception using errcode = '55000', message = 'e483_native_condition_after_send_missing',
                detail = v_function;
        end if;
        if position(c_marker in pg_get_functiondef(v_function::regprocedure)) > 0 then
            raise exception using errcode = '55000', message = 'e483_native_condition_after_send_already_applied',
                detail = v_function;
        end if;
    end loop;

    -- 2. Las ramas.
    v_text := pg_get_functiondef(c_branches::regprocedure);
    if length(v_text) - length(replace(v_text, c_old_declare, '')) <> length(c_old_declare)
       or length(v_text) - length(replace(v_text, c_old_then, '')) <> length(c_old_then)
       or length(v_text) - length(replace(v_text, c_old_rest, '')) <> length(c_old_rest)
       or length(v_text) - length(replace(v_text, c_old_last, '')) <> length(c_old_last) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_payment_failure_branches_definition',
            detail = c_branches;
    end if;
    execute replace(replace(replace(replace(v_text, c_old_declare, c_new_declare), c_old_then, c_new_then),
                            c_old_rest, c_new_rest), c_old_last, c_new_last);

    -- 3. Lo que abre la base.
    v_text := pg_get_functiondef(c_unsupported::regprocedure);
    if length(v_text) - length(replace(v_text, c_old_unsupported, '')) <> length(c_old_unsupported) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_unsupported_definition',
            detail = c_unsupported;
    end if;
    execute replace(v_text, c_old_unsupported, c_new_unsupported);

    -- 4. El compilador.
    v_text := pg_get_functiondef(c_core::regprocedure);
    if length(v_text) - length(replace(v_text, c_old_core, '')) <> length(c_old_core) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_compile_policy_core_definition',
            detail = c_core;
    end if;
    execute replace(v_text, c_old_core, c_new_core);

    -- 5. Los envios.
    v_text := pg_get_functiondef(c_sends::regprocedure);
    if length(v_text) - length(replace(v_text, c_old_sends, '')) <> length(c_old_sends) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_sends_definition',
            detail = c_sends;
    end if;
    execute replace(v_text, c_old_sends, c_new_sends);

    -- 6. El contexto del paso (security definer, de service_role: los permisos no cambian).
    v_text := pg_get_functiondef(c_context::regprocedure);
    if length(v_text) - length(replace(v_text, c_old_context_branches, '')) <> length(c_old_context_branches)
       or length(v_text) - length(replace(v_text, c_old_context_send, '')) <> length(c_old_context_send) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_execution_context_definition',
            detail = c_context;
    end if;
    select coalesce(p.proacl::text, ''), p.prosecdef into v_acl, v_secdef
      from pg_proc p where p.oid = c_context::regprocedure;
    execute replace(replace(v_text, c_old_context_branches, c_new_context_branches),
                    c_old_context_send, c_new_context_send);
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_context::regprocedure) is distinct from v_acl
       or (select p.prosecdef from pg_proc p where p.oid = c_context::regprocedure) is distinct from v_secdef then
        raise exception using errcode = '55000', message = 'e483_native_condition_after_send_acl_changed',
            detail = c_context;
    end if;

    foreach v_function in array array[c_branches, c_unsupported, c_core, c_sends, c_context] loop
        if position(c_marker in pg_get_functiondef(v_function::regprocedure)) = 0 then
            raise exception using errcode = '55000', message = 'e483_native_condition_after_send_not_applied',
                detail = v_function;
        end if;
    end loop;
end;
$e483$;

commit;
