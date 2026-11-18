-- flow-audience-start-reasons-v1: cada envio a una audiencia dice cuantas
-- personas arrancaron, cuantas no y por que (E-666, 2026-11-17).
-- Contrato: docs/contracts/flow-audience-trigger-v1.md («Lo que dice cada envio»).
--
-- Por que. Maribel, 2026-11-16 17:09Z: la ocurrencia 8ed2f5be... del flujo
-- aviso_manual_prueba termino con su unica persona liberada y
-- open_outcome = no_arranca; el motivo (conversacion_viva) quedo en
-- flow_engine_start_rejections, atado al evento sintetico de esa persona
-- (webhook_events system / FLOW_AUDIENCE_RELEASED, 20261112210600). La pagina
-- de envios (/flow-approval/envios) mostraba «Terminado» con «Salieron 1; el
-- motor abrio el envio a 0», sin decir por que: ninguna lectura de la pagina
-- devolvia el resultado de cada persona ni el motivo, y la tabla de rechazos
-- no la lee nadie mas que el aviso por flujo del motor (sin la ocurrencia).
--
-- Que cambia. _flow_audience_occurrence_rows (la usan
-- approver_read_flow_audience_v1, de la pagina, y
-- read_flow_audience_occurrences_v1, del bridge) suma cuatro claves a cada
-- ocurrencia, sin tocar las de antes:
--   * not_started_count: las liberadas con open_outcome = no_arranca.
--   * no_flow_count: las liberadas con open_outcome = sin_flujo (el flujo se
--     apago o cambio de version entre soltar y abrir).
--   * unsettled_count: las liberadas que el motor todavia no asento.
--   * not_started_by_reason: {motivo: cuantas} de las no_arranca. El motivo
--     de cada persona es el rechazo mas nuevo (last_at) del flujo con
--     source_ref = el evento sintetico de esa persona y esa ocurrencia
--     (external_event_id flow_audience:<flujo>:<version>:<ocurrencia>:<contacto>,
--     unico por (source, external_event_id)). Sin rechazo registrado (el bridge
--     recibio un error permanente de la admision), sin_registro.
-- Sin telefonos, sin contactos y sin nombres: solo conteos y codigos.
--
-- Como. create or replace con la misma firma, el mismo tipo de retorno y los
-- mismos atributos (sql, stable, security invoker, search_path): el dueno y los
-- permisos (ninguno fuera del dueno, 20261112220000) quedan como estan. Las
-- dos funciones que la llaman son security definer, asi que lee
-- webhook_events y flow_engine_start_rejections con el dueno, como ya leia
-- flow_audience_members.
--
-- Idempotente: con la marca ya puesta no hace nada. Con una definicion que no
-- es la de 20261112220000 (md5 del cuerpo, igual en maribel_db y en att1-db
-- el 17/11) aborta con 55000 sin cambiar nada.
--
-- Orden de despliegue: indistinto. Un bridge viejo ignora las claves nuevas;
-- el bridge nuevo sin esta migracion muestra lo de antes.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $flow_audience_start_reasons$
declare
    c_signature constant text := 'public._flow_audience_occurrence_rows(text,text,integer)';
    c_old_md5 constant text := 'fbddc394830be11898b98dc144b8a6d6';
    c_mark constant text := 'flow_audience_start_reasons';
    v_oid oid;
    v_src text;
begin
    if to_regclass('public.flow_audience_occurrences') is null
       or to_regclass('public.flow_audience_members') is null
       or to_regclass('public.flow_engine_start_rejections') is null
       or to_regclass('public.webhook_events') is null then
        raise exception using errcode = '55000', message = 'flow_audience_start_reasons_prerequisite_missing';
    end if;
    v_oid := to_regprocedure(c_signature);
    if v_oid is null then
        raise exception using errcode = '55000', message = 'flow_audience_start_reasons_prerequisite_missing';
    end if;
    select p.prosrc into v_src from pg_proc p where p.oid = v_oid;
    if position(c_mark in v_src) > 0 then
        raise notice 'flow_audience_start_reasons: ya aplicada';
        return;
    end if;
    if md5(v_src) is distinct from c_old_md5 then
        raise exception using errcode = '55000', message = 'unexpected_flow_audience_occurrence_rows_definition',
            detail = c_signature;
    end if;

    create or replace function public._flow_audience_occurrence_rows(p_tenant_ref text, p_ally_ref text,
                                                                     p_limit integer)
    returns jsonb
    language sql
    stable
    security invoker
    set search_path = pg_catalog, public, pg_temp
    as $function$
    -- flow_audience_start_reasons (20261117000700): ademas de los conteos de antes, cuantas
    -- arrancaron, cuantas no y por que (el rechazo mas nuevo del evento de cada persona).
    select coalesce((
        select jsonb_agg(row_to_json(r)::jsonb order by r.materialized_at desc)
          from (
            select o.id as occurrence_id, o.flow_id, c.flow_key, c.product_ref, o.flow_version, o.occurrence_key,
                   o.form, o.state, o.scheduled_at, o.member_count, o.omitted_count, o.per_hour, o.per_occurrence,
                   o.brake_reason, o.materialized_by, o.materialized_at, o.confirmed_by, o.confirmed_at,
                   o.finished_at,
                   (select count(*) from public.flow_audience_members m
                     where m.occurrence_id = o.id and m.state = 'liberada')::integer as released_count,
                   (select count(*) from public.flow_audience_members m
                     where m.occurrence_id = o.id and m.open_outcome = 'arranco')::integer as started_count,
                   (select coalesce(jsonb_object_agg(x.omit_reason, x.n), '{}'::jsonb)
                      from (select m.omit_reason, count(*) as n from public.flow_audience_members m
                             where m.occurrence_id = o.id and m.state = 'omitida' group by m.omit_reason) x)
                       as omitted_by_reason,
                   (select count(*) from public.flow_audience_members m
                     where m.occurrence_id = o.id and m.open_outcome = 'no_arranca')::integer as not_started_count,
                   (select count(*) from public.flow_audience_members m
                     where m.occurrence_id = o.id and m.open_outcome = 'sin_flujo')::integer as no_flow_count,
                   (select count(*) from public.flow_audience_members m
                     where m.occurrence_id = o.id and m.state = 'liberada' and m.opened_at is null)::integer
                       as unsettled_count,
                   (select coalesce(jsonb_object_agg(y.reason, y.n), '{}'::jsonb)
                      from (select coalesce(rejection.reason, 'sin_registro') as reason, count(*) as n
                              from public.flow_audience_members m
                              left join public.webhook_events w
                                on w.source = 'system'
                               and w.external_event_id = 'flow_audience:' || o.flow_id::text || ':'
                                   || o.flow_version::text || ':' || o.id::text || ':' || m.contact_id::text
                              left join lateral (
                                  select s.reason from public.flow_engine_start_rejections s
                                   where s.flow_id = o.flow_id and s.source_ref = w.id
                                   order by s.last_at desc, s.reason
                                   limit 1) rejection on true
                             where m.occurrence_id = o.id and m.open_outcome = 'no_arranca'
                             group by 1) y)
                       as not_started_by_reason
              from public.flow_audience_occurrences o
              join public.flow_controls c on c.flow_id = o.flow_id
             where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
             order by o.materialized_at desc
             limit p_limit
          ) r
    ), '[]'::jsonb)
    $function$;

    -- La firma, los atributos y los permisos quedan como estaban.
    if not exists (
        select 1 from pg_proc p join pg_language l on l.oid = p.prolang
         where p.oid = to_regprocedure(c_signature) and p.oid = v_oid and l.lanname = 'sql'
           and p.provolatile = 's' and not p.prosecdef
           and p.proconfig = array['search_path=pg_catalog, public, pg_temp']
           and pg_get_function_result(p.oid) = 'jsonb'
           and position(c_mark in p.prosrc) > 0) then
        raise exception using errcode = '55000', message = 'flow_audience_start_reasons_not_applied';
    end if;
    if exists (
        select 1 from pg_proc p cross join lateral aclexplode(p.proacl) acl
         where p.oid = v_oid and acl.grantee <> p.proowner) then
        raise exception using errcode = '55000', message = 'flow_audience_start_reasons_acl_changed';
    end if;
end;
$flow_audience_start_reasons$;

commit;
