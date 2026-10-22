-- flow-engine-reads-see-motor-v1: tres lecturas cuentan tambien lo que
-- autoriza el motor (flow_engine_request_authorizations), ademas de lo del
-- legado (pilot_outbound_request_authorizations):
--   * read_flow_engine_history_v1, la historia de la simulacion (contrato
--     docs/contracts/flow-engine-v1.md, seccion 2);
--   * read_automation_expert_flows_v1 (/flows del AE) y
--     read_automation_expert_lead_history_v1 (la historia de un lead), las
--     lecturas del AE (docs/contracts/automation-expert-read-v1.md).
--
-- Por que: desde el 22/10 22:38:35Z la ruta del carrito de ATT1 esta en
-- motor, y el motor autoriza en su propia tabla (20261022000100, la T2.3b).
-- Las tres lecturas contaban solo la del legado: la simulacion de una
-- propuesta no veia los envios del motor en la bolsa, y el AE no los mostraba
-- (ni en el consumo del dia, ni en lo observado, ni en la historia del lead).
-- El freno ya estaba bien: el legado suma las del motor con pool = scope_key
-- (20261021000200, flow_engine_shared_pool) y el motor suma las dos
-- (20261022000100). Esta migracion copia ese criterio en lo que se ve: una
-- autorizacion del motor cuenta para el scope del legado cuando su pool es
-- ese scope_key, y para una accion cuando es de esa accion.
--
-- Como: el metodo de 20261021000300 (sobre pg_get_functiondef). Cada cambio
-- es un ancla que tiene que aparecer exactamente una vez en la definicion
-- vigente, y su reemplazo lleva la marca flow_engine_reads_see_motor_v1. Si
-- un ancla falta o ya esta la marca, la migracion aborta sin tocar nada. El
-- resto de cada funcion queda letra por letra. execute de la definicion es un
-- create or replace: conserva la firma, el duenio y los permisos, y la
-- migracion verifica las dos ultimas cosas.
--
-- Lo que hace al aplicarse: solo reemplaza esas tres funciones. Sin filas del
-- motor, las tres devuelven lo mismo que antes. En la historia, una
-- autorizacion del motor sale con la misma forma que una del legado, con
-- scope_key = pool y scope_version y runtime_generation en null (el motor no
-- tiene version de scope ni generacion del control del legado). En la
-- historia del lead, el momento start_authorized de una del motor tiene la
-- evidencia en flow_engine_request_authorizations y el codigo
-- flow_engine_request_start_authorized.

begin;

set local lock_timeout = '5s';

do $reads$
declare
    c_marker constant text := 'flow_engine_reads_see_motor_v1';
    -- (firma, ancla, reemplazo): el orden es el de cada funcion.
    v_signatures text[] := array[]::text[];
    v_anchors text[] := array[]::text[];
    v_replacements text[] := array[]::text[];
    v_signature text;
    v_definition text;
    v_acl_before text;
    v_acl_after text;
    v_done text[] := array[]::text[];
    i integer;
begin
    -- -----------------------------------------------------------------------
    -- 1. read_flow_engine_history_v1: los contactos, el conteo y la lista.
    -- -----------------------------------------------------------------------
    v_signatures := v_signatures || 'public.read_flow_engine_history_v1(jsonb,text,timestamp with time zone)'::text;
    v_anchors := v_anchors || $a$                    union select a.contact_id from public.pilot_outbound_request_authorizations a
                           where a.scope_key = any((select v.scope_keys from v)::text[]))
$a$::text;
    v_replacements := v_replacements || $r$                    union select a.contact_id from public.pilot_outbound_request_authorizations a
                           where a.scope_key = any((select v.scope_keys from v)::text[])
                    -- flow_engine_reads_see_motor_v1: las del motor, con pool = scope_key.
                    union select m.contact_id from public.flow_engine_request_authorizations m
                           where m.pool = any((select v.scope_keys from v)::text[]))
$r$::text;

    v_signatures := v_signatures || 'public.read_flow_engine_history_v1(jsonb,text,timestamp with time zone)'::text;
    v_anchors := v_anchors || $a$          'autorizaciones', (select count(*) from public.pilot_outbound_request_authorizations a
                              where a.scope_key = any((select v.scope_keys from v)::text[])),
$a$::text;
    v_replacements := v_replacements || $r$          'autorizaciones', (select count(*) from public.pilot_outbound_request_authorizations a
                              where a.scope_key = any((select v.scope_keys from v)::text[]))
                            -- flow_engine_reads_see_motor_v1: las del motor, con pool = scope_key.
                            + (select count(*) from public.flow_engine_request_authorizations m
                                where m.pool = any((select v.scope_keys from v)::text[])),
$r$::text;

    v_signatures := v_signatures || 'public.read_flow_engine_history_v1(jsonb,text,timestamp with time zone)'::text;
    v_anchors := v_anchors || $a$      ) order by a.authorized_at, a.id), '[]'::json) from public.pilot_outbound_request_authorizations a
     where a.scope_key = any((select v.scope_keys from v)::text[])),
$a$::text;
    v_replacements := v_replacements || $r$      ) order by a.authorized_at, a.id), '[]'::json)
      -- flow_engine_reads_see_motor_v1: las del motor con la misma forma,
      -- scope_key = pool, y scope_version y runtime_generation en null.
      from (select l.id, l.scope_key, l.scope_version, l.action_id, l.contact_id, l.local_budget_date,
                   l.runtime_generation, l.authorized_at
              from public.pilot_outbound_request_authorizations l
            union all
            select m.id, m.pool, null::integer, m.action_id, m.contact_id, m.local_budget_date,
                   null::bigint, m.authorized_at
              from public.flow_engine_request_authorizations m) a
     where a.scope_key = any((select v.scope_keys from v)::text[])),
$r$::text;

    -- -----------------------------------------------------------------------
    -- 2. read_automation_expert_flows_v1: el consumo del dia y el total de
    -- cada scope, y los arranques de lo observado.
    -- -----------------------------------------------------------------------
    v_signatures := v_signatures || 'public.read_automation_expert_flows_v1(jsonb)'::text;
    v_anchors := v_anchors || $a$            'starts_today', (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where scope.scope_key is not null
                   and auth.scope_key = configured.scope_key
                   and auth.local_budget_date = (now() at time zone coalesce(scope.timezone, v_tz))::date
            ),
            'starts_total', (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where scope.scope_key is not null
                   and auth.scope_key = configured.scope_key
            )
$a$::text;
    v_replacements := v_replacements || $r$            'starts_today', (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where scope.scope_key is not null
                   and auth.scope_key = configured.scope_key
                   and auth.local_budget_date = (now() at time zone coalesce(scope.timezone, v_tz))::date
            ) + (
                -- flow_engine_reads_see_motor_v1: las del motor, con pool = scope_key.
                select count(*)
                  from public.flow_engine_request_authorizations engine_row
                 where scope.scope_key is not null
                   and engine_row.pool = configured.scope_key
                   and engine_row.local_budget_date = (now() at time zone coalesce(scope.timezone, v_tz))::date
            ),
            'starts_total', (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where scope.scope_key is not null
                   and auth.scope_key = configured.scope_key
            ) + (
                select count(*)
                  from public.flow_engine_request_authorizations engine_row
                 where scope.scope_key is not null
                   and engine_row.pool = configured.scope_key
            )
$r$::text;

    v_signatures := v_signatures || 'public.read_automation_expert_flows_v1(jsonb)'::text;
    v_anchors := v_anchors || $a$        select actions.flow_key, count(*) as n
          from public.pilot_outbound_request_authorizations auth
          join actions on actions.id = auth.action_id
$a$::text;
    v_replacements := v_replacements || $r$        select actions.flow_key, count(*) as n
          from (select legacy_row.action_id, legacy_row.authorized_at
                  from public.pilot_outbound_request_authorizations legacy_row
                union all
                -- flow_engine_reads_see_motor_v1: las del motor, por su accion.
                select engine_row.action_id, engine_row.authorized_at
                  from public.flow_engine_request_authorizations engine_row) auth
          join actions on actions.id = auth.action_id
$r$::text;

    -- -----------------------------------------------------------------------
    -- 3. read_automation_expert_lead_history_v1: el arranque rechazado, los
    -- topes deducidos, el momento autorizado y el consumo de ahora.
    -- -----------------------------------------------------------------------
    v_signatures := v_signatures || 'public.read_automation_expert_lead_history_v1(jsonb,uuid)'::text;
    v_anchors := v_anchors || $a$           and not exists (
               select 1 from public.pilot_outbound_request_authorizations auth
                where auth.attempt_id = attempt.id
           )
$a$::text;
    v_replacements := v_replacements || $r$           and not exists (
               select 1 from public.pilot_outbound_request_authorizations auth
                where auth.attempt_id = attempt.id
           )
           -- flow_engine_reads_see_motor_v1: lo que autorizo el motor tampoco es un rechazo.
           and not exists (
               select 1 from public.flow_engine_request_authorizations engine_row
                where engine_row.attempt_id = attempt.id
           )
$r$::text;

    v_signatures := v_signatures || 'public.read_automation_expert_lead_history_v1(jsonb,uuid)'::text;
    v_anchors := v_anchors || $a$               (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = rejected.scope_key
                   and auth.authorized_at <= rejected.first_at
               ) as total_before,
               (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = rejected.scope_key
                   and auth.local_budget_date = (rejected.first_at at time zone rejected.scope_tz)::date
                   and auth.authorized_at <= rejected.first_at
               ) as day_before
$a$::text;
    v_replacements := v_replacements || $r$               (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = rejected.scope_key
                   and auth.authorized_at <= rejected.first_at
               ) + (
                -- flow_engine_reads_see_motor_v1: las del motor, con pool = scope_key.
                select count(*)
                  from public.flow_engine_request_authorizations engine_row
                 where engine_row.pool = rejected.scope_key
                   and engine_row.authorized_at <= rejected.first_at
               ) as total_before,
               (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = rejected.scope_key
                   and auth.local_budget_date = (rejected.first_at at time zone rejected.scope_tz)::date
                   and auth.authorized_at <= rejected.first_at
               ) + (
                select count(*)
                  from public.flow_engine_request_authorizations engine_row
                 where engine_row.pool = rejected.scope_key
                   and engine_row.local_budget_date = (rejected.first_at at time zone rejected.scope_tz)::date
                   and engine_row.authorized_at <= rejected.first_at
               ) as day_before
$r$::text;

    v_signatures := v_signatures || 'public.read_automation_expert_lead_history_v1(jsonb,uuid)'::text;
    v_anchors := v_anchors || $a$          from public.pilot_outbound_request_authorizations auth
          join actions action on action.id = auth.action_id
        -- 11. Los arranques rechazados: un bucle, un momento, con el motivo deducido.
$a$::text;
    v_replacements := v_replacements || $r$          from public.pilot_outbound_request_authorizations auth
          join actions action on action.id = auth.action_id
        -- 10b. flow_engine_reads_see_motor_v1: los arranques que autorizo el motor.
        union all
        select engine_row.authorized_at, 'start_authorized', action.flow_key, action.flow_version,
               'flow_engine_request_start_authorized', null, 'stored', 'case',
               'flow_engine_request_authorizations', engine_row.id::text,
               jsonb_build_object('local_budget_date', engine_row.local_budget_date)
          from public.flow_engine_request_authorizations engine_row
          join actions action on action.id = engine_row.action_id
        -- 11. Los arranques rechazados: un bucle, un momento, con el motivo deducido.
$r$::text;

    v_signatures := v_signatures || 'public.read_automation_expert_lead_history_v1(jsonb,uuid)'::text;
    v_anchors := v_anchors || $a$               (
                select count(*) from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = action.scope_key
               ) as total_now,
               (
                select count(*) from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = action.scope_key
                   and auth.local_budget_date = (now() at time zone action.scope_tz)::date
               ) as day_now,
$a$::text;
    v_replacements := v_replacements || $r$               (
                select count(*) from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = action.scope_key
               ) + (
                -- flow_engine_reads_see_motor_v1: las del motor, con pool = scope_key.
                select count(*) from public.flow_engine_request_authorizations engine_row
                 where engine_row.pool = action.scope_key
               ) as total_now,
               (
                select count(*) from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = action.scope_key
                   and auth.local_budget_date = (now() at time zone action.scope_tz)::date
               ) + (
                select count(*) from public.flow_engine_request_authorizations engine_row
                 where engine_row.pool = action.scope_key
                   and engine_row.local_budget_date = (now() at time zone action.scope_tz)::date
               ) as day_now,
$r$::text;

    -- -----------------------------------------------------------------------
    -- Aplicar: una funcion por vez, con todas sus anclas.
    -- -----------------------------------------------------------------------
    for i in 1 .. array_length(v_signatures, 1) loop
        v_signature := v_signatures[i];
        if v_signature = any(v_done) then
            continue;
        end if;
        if to_regprocedure(v_signature) is null then
            raise exception using errcode = '55000', message = 'flow_engine_reads_function_missing',
                detail = v_signature;
        end if;
        if (select p.proowner from pg_proc p where p.oid = v_signature::regprocedure)
           is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
            raise exception using errcode = '55000', message = 'flow_engine_reads_owner_mismatch',
                detail = v_signature;
        end if;
        v_definition := pg_get_functiondef(v_signature::regprocedure);
        if position(c_marker in v_definition) > 0 then
            raise exception using errcode = '55000', message = 'unexpected_flow_engine_reads_definition',
                detail = v_signature || ': ya lleva la marca';
        end if;
        select coalesce(p.proacl::text, '') into v_acl_before
          from pg_proc p where p.oid = v_signature::regprocedure;
        for j in i .. array_length(v_signatures, 1) loop
            continue when v_signatures[j] <> v_signature;
            if length(v_definition) - length(replace(v_definition, v_anchors[j], '')) <> length(v_anchors[j]) then
                raise exception using errcode = '55000', message = 'unexpected_flow_engine_reads_definition',
                    detail = v_signature || ': el ancla ' || j || ' no aparece exactamente una vez';
            end if;
            v_definition := replace(v_definition, v_anchors[j], v_replacements[j]);
        end loop;

        execute v_definition;

        v_definition := pg_get_functiondef(v_signature::regprocedure);
        for j in i .. array_length(v_signatures, 1) loop
            continue when v_signatures[j] <> v_signature;
            if position(v_replacements[j] in v_definition) = 0 then
                raise exception using errcode = '55000', message = 'flow_engine_reads_not_replaced',
                    detail = v_signature || ': el reemplazo ' || j;
            end if;
        end loop;
        select coalesce(p.proacl::text, '') into v_acl_after
          from pg_proc p where p.oid = v_signature::regprocedure;
        if v_acl_after is distinct from v_acl_before then
            raise exception using errcode = '55000', message = 'flow_engine_reads_acl_changed',
                detail = v_signature;
        end if;
        v_done := v_done || v_signature;
    end loop;
    if array_length(v_done, 1) <> 3 then
        raise exception using errcode = '55000', message = 'flow_engine_reads_not_replaced',
            detail = array_to_string(v_done, ', ');
    end if;
end;
$reads$;

commit;
