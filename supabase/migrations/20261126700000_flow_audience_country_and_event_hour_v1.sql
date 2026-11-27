-- flow-audience-country-and-event-hour-v1: el disparo por audiencia segmenta por
-- pais y dice la hora del evento en la zona de cada persona (4.5 y 4.4 de la lista
-- definitiva, 2026-11-26). Contrato: docs/contracts/flow-audience-trigger-v1.md
-- (0.8.0).
--
-- Por que. El recordatorio de la clase del 22/11 de Maribel
-- (maribel_forever_clase_recordatorio_01) salio a 1.704 personas de 24 indicativos
-- con una sola hora escrita, «7:00 p. m. (hora Colombia)»: a Mexico, Argentina
-- o Espana les tocaba hacer la cuenta. El flujo de audiencia ya sabe a quien le
-- llega (para); le faltaba elegir por pais (para.paises, para.excluye_paises: lo
-- filtra el bridge, flow_audience.select_members) y escribir la hora del evento
-- en la zona de cada una (la variable hora_evento, que llena el bridge).
--
-- Que cambia:
--   1. _flow_engine_native_steps('nativo:audiencia'): los tres toques admiten
--      hora_evento en variables_de, ademas de nombre y producto. El compilador
--      de la politica dejaba afuera (variables_differ) un paso con esa
--      variable. Las otras formas no cambian, y un paso con nombre y producto
--      compila como antes.
--   2. approver_read_flow_audience_countries_v1 (nueva, de la pagina, rol
--      flow_change_approver, con la sesion de «Entrar con Slack»): por cada
--      una de las 50 ocurrencias mas nuevas de la instancia, cuantas personas
--      de la lista (pendientes o liberadas, no las omitidas) hay por cada
--      prefijo de cuatro digitos del telefono. El bridge lo traduce a pais con
--      la misma regla con que filtro (bridge.phones.phone_prefix_country, que
--      solo mira esos cuatro digitos). Sin telefonos: cuatro digitos son el
--      indicativo y, en +1, el codigo de area.
--
-- Lo que NO cambia. Ninguna tabla, ninguna otra funcion y ningun permiso. Un
-- flujo sin hora_evento compila igual; la lista, el armado y la admision no
-- miran para.paises (lo filtra el bridge al elegir a quien mandar a armar).
--
-- Como: el metodo de 20261112210600 para el punto 1 (reemplazo contado sobre la
-- definicion VIGENTE, con su marca, conservando duenio y permisos). Correrla
-- dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 28000 sesion invalida; 55000 un estado que
-- no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_engine_native_steps(text)') is null
       or to_regclass('public.flow_audience_occurrences') is null
       or to_regclass('public.flow_audience_members') is null
       or to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regrole('flow_change_approver') is null
       or position('e483_native_audience_steps' in pg_get_functiondef(
              'public._flow_engine_native_steps(text)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_audience_country_and_event_hour_missing';
    end if;
    if to_regprocedure('public.approver_read_flow_audience_countries_v1(jsonb,text)') is not null
       or position('flow_audience_event_hour_steps' in pg_get_functiondef(
              'public._flow_engine_native_steps(text)'::regprocedure)) > 0 then
        raise exception using errcode = '55000', message = 'flow_audience_country_and_event_hour_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. hora_evento en los tres toques de la audiencia.
-- ===========================================================================
do $event_hour$
declare
    c_signature constant text := 'public._flow_engine_native_steps(text)';
    c_start constant text := '-- e483_native_audience_steps:';
    c_end constant text := '-- flow_engine_native_intent:';
    c_old constant text := '"variables_de": ["nombre", "producto"]';
    c_new constant text := '"variables_de": ["nombre", "producto", "hora_evento"]';
    v_definition text;
    v_acl text;
    v_head text;
    v_block text;
    v_tail text;
    v_at integer;
    v_end integer;
begin
    if (select p.proowner from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_audience_event_hour_owner_mismatch';
    end if;
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    v_at := position(c_start in v_definition);
    if v_at = 0 or length(v_definition) - length(replace(v_definition, c_start, '')) <> length(c_start) then
        raise exception using errcode = '55000', message = 'unexpected_flow_audience_event_hour_definition';
    end if;
    v_head := left(v_definition, v_at - 1);
    v_block := substr(v_definition, v_at);
    v_end := position(c_end in substr(v_block, length(c_start) + 1));
    if v_end = 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_audience_event_hour_definition';
    end if;
    v_tail := substr(v_block, length(c_start) + v_end);
    v_block := left(v_block, length(c_start) + v_end - 1);
    -- Los tres toques de la audiencia: first_contact, native_touch_2 y native_touch_3.
    if (length(v_block) - length(replace(v_block, c_old, ''))) / length(c_old) <> 3 then
        raise exception using errcode = '55000', message = 'unexpected_flow_audience_event_hour_definition';
    end if;
    v_block := replace(v_block, c_old, c_new);
    v_block := replace(v_block, c_start,
        '-- flow_audience_event_hour_steps: hora_evento en los tres toques (20261126700000).' || chr(10)
        || '        ' || c_start);
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = c_signature::regprocedure;
    execute v_head || v_block || v_tail;
    if position('flow_audience_event_hour_steps' in pg_get_functiondef(c_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_audience_event_hour_not_replaced';
    end if;
    -- create or replace conserva el duenio y los permisos: se verifica.
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from v_acl then
        raise exception using errcode = '55000', message = 'flow_audience_event_hour_acl_changed';
    end if;
end;
$event_hour$;

-- ===========================================================================
-- 2. La lista por prefijo, para el conteo por pais de la pagina.
-- ===========================================================================

create function public.approver_read_flow_audience_countries_v1(p_instance jsonb, p_session_hash text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return jsonb_build_object(
        'contract', 'flow-audience-countries-v1',
        'occurrences', coalesce((
            select jsonb_object_agg(o.id::text, coalesce((
                       select jsonb_object_agg(x.prefix, x.n)
                         from (select left(m.normalized_phone, 4) as prefix, count(*)::integer as n
                                 from public.flow_audience_members m
                                where m.occurrence_id = o.id and m.state <> 'omitida'
                                group by 1) x), '{}'::jsonb))
              from (select o.id from public.flow_audience_occurrences o
                      join public.flow_controls c on c.flow_id = o.flow_id
                     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
                     order by o.materialized_at desc
                     limit 50) o), '{}'::jsonb));
end;
$function$;

do $roles$
declare
    v_role text;
begin
    execute 'revoke all on function public.approver_read_flow_audience_countries_v1(jsonb, text) from public';
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role', 'flow_change_approver') order by 1
    loop
        execute format('revoke all on function public.approver_read_flow_audience_countries_v1(jsonb, text) from %I',
                       v_role);
    end loop;
    if to_regrole('flow_change_approver') is not null then
        grant execute on function public.approver_read_flow_audience_countries_v1(jsonb, text) to flow_change_approver;
    end if;
end;
$roles$;

commit;
