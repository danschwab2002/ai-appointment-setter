-- flow-engine-native-silence-sweep-v1: lo que el barrido nativo del
-- seguimiento a 24 h con cupon lee de la base (E-269, P22-3 de M16; diseno:
-- documentacion/2026-11-01-m16-diseno-seguimiento-y-ficha-nativos.md, 1.2 (c)).
--
-- Por que. P22-2 (20261101000200) dejo publicar el seguimiento como un silencio
-- nativo (dispara.silencio con lo_ultimo: agente) y le dio su reserva,
-- claim_flow_engine_silence_v1, pero nadie la llamaba: con un silencio nativo
-- prendido o en cohorte, la barrera del legado (blocked_native_flow) le saca la
-- conversacion al barrido de hoy y la cohorte se queda sin cupon (la advertencia
-- de E-251). El barrido nativo (src/bridge/native_silence.py) ejecuta la
-- definicion publicada, y para eso necesita leerla entera: la plantilla, el
-- idioma y el cupon del paso, la ventana del silencio, las conversaciones, el
-- horario, la version y su sha256 (los argumentos de la reserva). La lectura de
-- los flujos nativos de hoy (read_flow_engine_native_flows_v1) da solo el
-- evento de la version activa, y un silencio no tiene evento.
--
-- Que hace. Una sola funcion nueva, read_flow_engine_native_silences_v1
-- (stable, security definer, solo service_role): los silencios del agente de la
-- instancia que no estan apagados ni pausados (prendido o cohorte), con ruta
-- nativa, con la definicion de su version activa, su sha256 y, en cohorte, las
-- conversaciones de Chatwoot de los contactos de la cohorte (el barrido en
-- cohorte relee solo esas: no duplica la carga sobre Chatwoot). Prendido, la
-- lista es null: es todo el inbox.
--
-- Silencio del agente es lo que dice _flow_engine_native_trigger: el mismo
-- criterio de la reserva nativa y de la barrera del legado. Si otra migracion
-- reemplazo entero el disparador sin el silencio (el reemplazo contado de
-- 20261101000200), la lectura falla fuerte con 55000, como la reserva nativa:
-- el bridge la ve failing en /ready, y el legado, sin el silencio en el
-- disparador, vuelve a mandar.
--
-- Lo que no hace: no redefine ninguna funcion, no crea tablas ni roles, no
-- escribe filas y no cambia el ACL de lo que ya existe. Correrla dos veces
-- aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida (la instancia, por _flow_store_instance);
-- 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_engine_native_trigger(jsonb)') is null
       or to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public.claim_flow_engine_silence_v1(uuid,integer,text,bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)') is null
       or to_regclass('public.flow_cohort_members') is null
       or to_regclass('public.flow_engine_routes') is null
       or position('flow_engine_native_silence_trigger'
                   in pg_get_functiondef('public._flow_engine_native_trigger(jsonb)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_sweep_missing';
    end if;
    if to_regprocedure('public.read_flow_engine_native_silences_v1(jsonb)') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_sweep_already_applied';
    end if;
end;
$already$;

-- Los silencios del agente de la instancia que corren (prendido o cohorte).
create function public.read_flow_engine_native_silences_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_flows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    -- flow_engine_native_silence_sweep_trigger_guard: el mismo freno que la reserva
    -- nativa. Sin el silencio en el disparador, falla fuerte.
    if position('flow_engine_native_silence_trigger' in
                pg_get_functiondef('public._flow_engine_native_trigger(jsonb)'::regprocedure)) = 0 then
        raise exception using errcode = '55000',
            message = 'flow_engine_native_silence_trigger_missing';
    end if;
    select coalesce(jsonb_agg(jsonb_build_object(
               'flow_id', c.flow_id,
               'product_ref', c.product_ref,
               'flow_key', c.flow_key,
               'state', c.state,
               'active_version', c.active_version,
               'definition_sha256', d.definition_sha256,
               'definition', d.definition,
               -- En cohorte, las conversaciones de Chatwoot de sus contactos; prendido, null.
               'cohort_conversation_ids', case when c.state = 'cohorte' then (
                   select coalesce(jsonb_agg(distinct (conversation.commercial_context ->> 'chatwoot_conversation_id')::bigint), '[]'::jsonb)
                     from public.flow_cohort_members member
                     join public.conversations conversation on conversation.contact_id = member.contact_id
                    where member.flow_id = c.flow_id
                      and conversation.commercial_context ->> 'chatwoot_conversation_id' ~ '^[1-9][0-9]{0,17}$')
               end)
               order by c.product_ref, c.flow_key), '[]'::jsonb)
      into v_flows
      from public.flow_controls c
      join public.flow_engine_routes r on r.flow_id = c.flow_id and r.legacy_kind is null
      join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.state in ('prendido', 'cohorte')
       and public._flow_engine_native_trigger(d.definition) = 'silencio_agente';
    return jsonb_build_object('contract', 'flow-engine-v2', 'flows', v_flows);
end;
$function$;

comment on function public.read_flow_engine_native_silences_v1(jsonb) is
    'flow_engine_native_silence_sweep (20261102000300): los silencios del agente de la instancia que corren, '
    'con su definicion activa, su sha256 y, en cohorte, las conversaciones de sus contactos.';

-- Los permisos: solo service_role (el bridge).
do $acl$
declare
    c_read constant regprocedure := 'public.read_flow_engine_native_silences_v1(jsonb)';
    v_role text;
begin
    execute format('revoke all on function %s from public', c_read);
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function %s from %I', c_read, v_role);
    end loop;
    if to_regrole('service_role') is not null then
        execute format('grant execute on function %s to service_role', c_read);
    end if;

    -- Por presencia: service_role la ejecuta y ningun rol de la API.
    if exists (select 1 from pg_roles where rolname = 'service_role'
                  and not has_function_privilege(rolname, c_read, 'EXECUTE'))
       or exists (select 1 from pg_roles where rolname in ('anon', 'authenticated')
                     and has_function_privilege(rolname, c_read, 'EXECUTE')) then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_sweep_acl_unexpected',
            detail = 'solo service_role ejecuta read_flow_engine_native_silences_v1';
    end if;
end;
$acl$;

commit;
