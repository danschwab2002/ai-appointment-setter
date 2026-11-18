-- Migration: la repeticion de un flujo de audiencia cuenta solo a quien arranco
-- un recorrido (E-665, 2026-11-17).
--
-- Por que. materialize_flow_audience_occurrence_v1 (20261112220000, E-484)
-- deja omitida como repetida a toda persona que ya fue liberada en otra
-- ocurrencia del mismo flujo dentro de topes.repeticion.otra_despues_de (7d
-- por defecto), haya arrancado o no su recorrido. Maribel, flujo
-- aviso_manual_prueba: en la ocurrencia del 16/11 (8ed2f5be...) el contacto de
-- prueba quedo liberada con open_outcome = no_arranca (la admision lo rechazo
-- por conversacion_viva, flow_engine_start_rejections), o sea no recibio nada;
-- en la del 17/11 (manual:a1d39ef1-0168-4f30-ac7b-11a97f2d54b9) quedo omitida
-- como repetida. Con clientas reales, quien estaba conversando cuando salio un
-- aviso se pierde ese aviso y todos los del flujo durante 7 dias, en silencio.
--
-- Que cambia. La repeticion mira solo las liberaciones que arrancaron
-- (open_outcome = arranco) o que todavia no se asentaron (open_outcome nulo:
-- el motor puede estar abriendo ese recorrido ahora mismo, y la base vuelve a
-- dar a esa persona a los 10 minutos). Una liberacion asentada como no_arranca
-- (el motor la rechazo: conversacion viva, tope por persona, prioridad,
-- permiso, baja, ya en curso) o sin_flujo (otra version activa) no consume la
-- repeticion. La baja no necesita contar: la lista ya la omite como baja antes
-- de mirar la repeticion. Nada mas cambia: la firma, el resultado, los otros
-- motivos y su orden, el ritmo, el asiento.
--
-- Como. create or replace con el cuerpo vivo y un reemplazo contado (una
-- linea mas en el exists de la repeticion). Se verifican antes y despues el
-- lenguaje, security definer, volatile, el search_path y los permisos (solo
-- service_role, de 20261112220000); create or replace los conserva.
--
-- Idempotente: si la funcion ya tiene la marca, no hace nada. Con otra
-- definicion (otra migracion la cambio y esta no la conoce), aborta con
-- unexpected_flow_audience_materialize_definition.
--
-- Sin cambio en el bridge: el bridge llama a la misma funcion con los mismos
-- argumentos. Se puede aplicar antes o despues de cualquier imagen.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $flow_audience_repeat_counts_started$
declare
    c_signature constant text :=
        'public.materialize_flow_audience_occurrence_v1(jsonb,uuid,integer,jsonb,jsonb)';
    c_old constant text :=
        E'             where o.flow_id = p_flow_id and m.contact_id = v_contact and m.state = ''liberada''\n'
        || E'               and (v_repeat is null or m.released_at > clock_timestamp() - v_repeat)\n';
    c_new constant text :=
        E'             where o.flow_id = p_flow_id and m.contact_id = v_contact and m.state = ''liberada''\n'
        || E'               -- flow_audience_repeat_counts_started: solo cuenta quien arranco o\n'
        || E'               -- todavia no se asento; no_arranca y sin_flujo no recibieron nada\n'
        || E'               -- (20261117000600, E-665).\n'
        || E'               and (m.open_outcome is null or m.open_outcome = ''arranco'')\n'
        || E'               and (v_repeat is null or m.released_at > clock_timestamp() - v_repeat)\n';
    c_mark constant text := 'flow_audience_repeat_counts_started';
    v_oid oid;
    v_src text;
    v_result text;
    v_lang text;
    v_secdef boolean;
    v_volatile "char";
    v_config text[];
    v_acl text;
begin
    v_oid := to_regprocedure(c_signature);
    if v_oid is null then
        raise exception using errcode = '55000', message = 'flow_audience_materialize_missing';
    end if;
    select p.prosrc, pg_get_function_result(p.oid), l.lanname, p.prosecdef, p.provolatile, p.proconfig,
           coalesce(p.proacl::text, '')
      into v_src, v_result, v_lang, v_secdef, v_volatile, v_config, v_acl
      from pg_proc p join pg_language l on l.oid = p.prolang
     where p.oid = v_oid;

    -- Ya aplicada: la marca y el reemplazo estan. No se toca nada.
    if length(v_src) - length(replace(v_src, c_new, '')) = length(c_new) then
        raise notice 'flow_audience_repeat_counts_started: ya aplicada';
        return;
    end if;

    -- La definicion vigente tiene que ser la de 20261112220000 (E-484).
    if v_result is distinct from 'jsonb'
       or v_lang is distinct from 'plpgsql'
       or not v_secdef
       or v_volatile is distinct from 'v'
       or v_config is distinct from array['search_path=pg_catalog, public, pg_temp']
       or position(c_mark in v_src) > 0
       or length(v_src) - length(replace(v_src, c_old, '')) <> length(c_old) then
        raise exception using errcode = '55000',
            message = 'unexpected_flow_audience_materialize_definition', detail = c_signature;
    end if;

    execute format(
        'create or replace function public.materialize_flow_audience_occurrence_v1('
        || 'p_instance jsonb, p_flow_id uuid, p_version integer, p_occurrence jsonb, p_members jsonb) '
        || 'returns jsonb language plpgsql volatile security definer '
        || 'set search_path = pg_catalog, public, pg_temp as %L',
        replace(v_src, c_old, c_new));

    -- Lo mismo de antes, salvo el reemplazo: atributos y permisos.
    if not exists (
        select 1 from pg_proc p join pg_language l on l.oid = p.prolang
         where p.oid = v_oid
           and l.lanname = 'plpgsql' and p.prosecdef and p.provolatile = 'v'
           and p.proconfig = array['search_path=pg_catalog, public, pg_temp']
           and coalesce(p.proacl::text, '') = v_acl
           and length(p.prosrc) - length(replace(p.prosrc, c_new, '')) = length(c_new)
    ) then
        raise exception using errcode = '55000',
            message = 'flow_audience_repeat_counts_started_verification_failed', detail = c_signature;
    end if;
end;
$flow_audience_repeat_counts_started$;

commit;
