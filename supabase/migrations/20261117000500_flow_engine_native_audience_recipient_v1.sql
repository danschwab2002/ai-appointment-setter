-- Migration: el caso de un flujo nativo de audiencia lleva al despacho el
-- producto del evento que lo abrio (E-661, 2026-11-17).
--
-- Por que. Ningun envio de un flujo nativo con disparo por audiencia (manual,
-- fecha, cada) salio en 30 dias, en ninguna instancia. Maribel, 2026-11-17
-- 16:01-16:10Z: aviso_fecha_prueba y aviso_diario_prueba (cohorte) liberaron
-- su ocurrencia y el motor arranco (casos 083dc040... y 602c59d3...); cada uno
-- termino con 4 intentos failed_before_request / pre_request_failed y la
-- accion en permanent_failed, con followup_recipient_not_allowlisted en el log
-- del bridge. El despachador valida al destinatario con
-- _is_authorized_followup_recipient (src/bridge/worker.py): sin JID fijo exige
-- que execution_context.offer_code este entre las ofertas aceptadas del
-- binding. Un caso de audiencia no tiene oferta por diseno (el evento del
-- opener de audiencia del bridge, audience_event, no la lleva) y
-- get_followup_execution_context la devuelve nula: el chequeo no puede pasar.
--
-- Que cambia. get_followup_execution_context devuelve una columna mas al
-- final, flow_engine_audience_product_ref: para un caso nativo (policy_key
-- flujo:...) sin offer_code abierto por un disparo de audiencia
-- (context.flow_engine_trigger = audiencia), el producto_ref del evento que lo
-- abrio (webhook_events system / FLOW_AUDIENCE_RELEASED, abandonment_event_id
-- del caso). La admision ya exigio que ese producto este atado a la instancia
-- (flow_store_product_not_bound). Para todo otro caso, nula. Las trece
-- columnas de antes no cambian. El bridge valida con esa columna el producto
-- contra los productos declarados de la instancia (funnel_ref y nombre); el
-- chequeo por oferta de los disparos por evento (compra, carrito, pago
-- rechazado, formulario) no cambia.
--
-- Como. Cambiar el tipo de retorno exige drop y create. El cuerpo es el vivo
-- con un reemplazo contado (la columna nueva al final del select); el
-- lenguaje, security invoker, volatile y el search_path se verifican antes y
-- se repiten igual. Los permisos (solo service_role, de
-- 20260812000100) se guardan antes del drop y se restauran; anon y
-- authenticated quedan sin execute aunque el default privileges de Supabase
-- se los de a una funcion nueva. Se verifica todo al final.
--
-- Idempotente: si la funcion ya tiene la columna y la marca, no hace nada.
-- Con otra definicion (otra migracion la cambio y esta no la conoce), aborta.
--
-- El bridge viejo ignora la columna nueva (lee las trece por nombre); el
-- bridge nuevo con una base sin esta migracion la lee nula y el caso de
-- audiencia se sigue frenando, ahora sin reintentos.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $flow_engine_native_audience_recipient$
declare
    c_signature constant text := 'public.get_followup_execution_context(uuid,text,bigint,timestamptz)';
    c_result_old constant text :=
        'TABLE(action_id uuid, action_type text, step_key text, recovery_case_id uuid, contact_id uuid, '
        || 'source_event_id uuid, buyer_name text, buyer_email text, buyer_phone text, product_name text, '
        || 'offer_code text, current_goal text, lead_stage text)';
    c_columns_new constant text :=
        'action_id uuid, action_type text, step_key text, recovery_case_id uuid, contact_id uuid, '
        || 'source_event_id uuid, buyer_name text, buyer_email text, buyer_phone text, product_name text, '
        || 'offer_code text, current_goal text, lead_stage text, flow_engine_audience_product_ref text';
    c_old constant text :=
        E'        rc.current_goal,\n'
        || E'        rc.lead_stage\n'
        || E'    from public.recovery_cases rc\n';
    c_new constant text :=
        E'        rc.current_goal,\n'
        || E'        rc.lead_stage,\n'
        || E'        -- flow_engine_native_audience_recipient: un caso nativo de audiencia\n'
        || E'        -- nace sin oferta; el despacho valida el producto del evento que lo\n'
        || E'        -- abrio (20261117000500).\n'
        || E'        case when rc.offer_code is null and rc.policy_key like ''flujo:%''\n'
        || E'                  and rc.context ->> ''flow_engine_trigger'' = ''audiencia''\n'
        || E'             then (select we.payload ->> ''producto_ref''\n'
        || E'                     from public.webhook_events we\n'
        || E'                    where we.id = rc.abandonment_event_id\n'
        || E'                      and we.source = ''system''\n'
        || E'                      and we.event_type = ''FLOW_AUDIENCE_RELEASED'')\n'
        || E'        end\n'
        || E'    from public.recovery_cases rc\n';
    c_mark constant text := 'flow_engine_native_audience_recipient';
    v_oid oid;
    v_src text;
    v_result text;
    v_lang text;
    v_secdef boolean;
    v_volatile "char";
    v_config text[];
    v_comment text;
    v_grantees text[];
    v_role text;
begin
    v_oid := to_regprocedure(c_signature);
    if v_oid is null then
        raise exception using errcode = '55000', message = 'followup_execution_context_missing';
    end if;
    select p.prosrc, pg_get_function_result(p.oid), l.lanname, p.prosecdef, p.provolatile, p.proconfig,
           obj_description(p.oid, 'pg_proc')
      into v_src, v_result, v_lang, v_secdef, v_volatile, v_config, v_comment
      from pg_proc p join pg_language l on l.oid = p.prolang
     where p.oid = v_oid;

    -- Ya aplicada: la columna y la marca estan. No se toca nada.
    if v_result = 'TABLE(' || c_columns_new || ')'
       and length(v_src) - length(replace(v_src, c_new, '')) = length(c_new) then
        raise notice 'flow_engine_native_audience_recipient: ya aplicada';
        return;
    end if;

    -- La definicion vigente tiene que ser la de 20261026000200 (E-122).
    if v_result is distinct from c_result_old
       or v_lang is distinct from 'plpgsql'
       or v_secdef
       or v_volatile is distinct from 'v'
       or v_config is distinct from array['search_path=public, pg_temp']
       or position('flow_engine_native_offer_in_context' in v_src) = 0
       or position(c_mark in v_src) > 0
       or length(v_src) - length(replace(v_src, c_old, '')) <> length(c_old) then
        raise exception using errcode = '55000',
            message = 'unexpected_followup_execution_context_definition', detail = c_signature;
    end if;

    -- Los permisos de hoy: los roles con execute que no son el dueno.
    select coalesce(array_agg(r.rolname::text order by r.rolname), array[]::text[])
      into v_grantees
      from pg_proc p
      cross join lateral aclexplode(p.proacl) acl
      join pg_roles r on r.oid = acl.grantee
     where p.oid = v_oid and acl.grantee <> p.proowner and acl.privilege_type = 'EXECUTE';

    execute format('drop function %s', c_signature);
    execute format(
        'create function public.get_followup_execution_context('
        || 'p_action_id uuid, p_worker_id text, p_lease_generation bigint, p_now timestamp with time zone) '
        || 'returns table (%s) language plpgsql volatile security invoker '
        || 'set search_path = public, pg_temp as %L',
        c_columns_new, replace(v_src, c_old, c_new));
    v_oid := to_regprocedure(c_signature);
    if v_comment is not null then
        execute format('comment on function %s is %L', c_signature, v_comment);
    end if;

    execute format('revoke all on function %s from public', c_signature);
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function %s from %I', c_signature, v_role);
    end loop;
    foreach v_role in array v_grantees loop
        execute format('grant execute on function %s to %I', c_signature, v_role);
    end loop;

    -- Verificacion: la columna, el cuerpo, los atributos y los mismos permisos.
    if (select pg_get_function_result(p.oid) from pg_proc p where p.oid = v_oid)
           is distinct from 'TABLE(' || c_columns_new || ')'
       or (select length(p.prosrc) - length(replace(p.prosrc, c_new, '')) from pg_proc p where p.oid = v_oid)
           <> length(c_new)
       or (select p.prosecdef or p.provolatile <> 'v' or p.proconfig is distinct from v_config
             from pg_proc p where p.oid = v_oid)
       or (select coalesce(array_agg(r.rolname::text order by r.rolname), array[]::text[])
             from pg_proc p
             cross join lateral aclexplode(p.proacl) acl
             join pg_roles r on r.oid = acl.grantee
            where p.oid = v_oid and acl.grantee <> p.proowner and acl.privilege_type = 'EXECUTE')
           is distinct from v_grantees
       or (to_regrole('anon') is not null and has_function_privilege('anon', v_oid, 'EXECUTE'))
       or (to_regrole('authenticated') is not null and has_function_privilege('authenticated', v_oid, 'EXECUTE'))
    then
        raise exception using errcode = '55000', message = 'flow_engine_native_audience_recipient_not_applied';
    end if;
end;
$flow_engine_native_audience_recipient$;

commit;
