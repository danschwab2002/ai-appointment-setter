-- agent-campaign-judge-v1: las corridas del juez de una campana que pide el
-- Automation Expert (4.9 de la lista definitiva, F-163; D14 y D-49 del mapa Z1).
-- Contrato: docs/contracts/agent-campaign-judge-v1.md.
--
-- Hoy el juez (python -m bridge.campaign_check) lo corre el operador a mano. Con
-- AUTOMATION_EXPERT_JUDGE_ENABLED lo pide el AE y lo corre el bridge en segundo
-- plano, con la misma funcion. Cada corrida llama al modelo tres veces: esta tabla
-- es lo que la acota y lo que el AE lee despues.
--
-- 1. Una tabla nueva, vacia: agent_campaign_judge_runs (una fila por corrida).
--    Sin permisos para nadie de la API, con RLS.
-- 2. Tres funciones del bridge, solo de service_role:
--    * start_agent_campaign_judge_run_v1: abre una corrida si la campana esta
--      validada, le quedan mas de 20 minutos, no hay otra corriendo en la
--      instancia y la clave de la campana tiene menos de p_daily_limit corridas
--      desde p_day_start (el comienzo del dia en la zona de la instancia, lo
--      calcula el bridge). Una corriendo hace mas de 30 minutos se da por
--      abandonada (el bridge se reinicio en el medio) y queda como error.
--    * finish_agent_campaign_judge_run_v1: cierra la corrida con su resultado.
--    * get_agent_campaign_judge_run_v1: la ultima corrida de la campana y
--      cuantas lleva hoy su clave.
--
-- La franja horaria (09:00 a 21:00 de la instancia) la mira el bridge antes de
-- llamar: fuera de ella no llega ningun pedido a la base.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- AUTOMATION_EXPERT_JUDGE_ENABLED el bridge no llama a ninguna de estas
-- funciones. Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe o no es de esta instancia.
-- Lo que el ciclo no permite (no validada, vence, tope, otra corriendo) vuelve
-- como {"ok": false, "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.agent_campaigns') is null
       or to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_store_actor_is_valid(text,text)') is null
       or to_regprocedure('public._agent_campaign_expire_if_due(public.agent_campaigns)') is null then
        raise exception using errcode = '55000', message = 'agent_campaign_judge_runs_prerequisite_missing';
    end if;
    if to_regclass('public.agent_campaign_judge_runs') is not null then
        raise exception using errcode = '55000', message = 'agent_campaign_judge_runs_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. La tabla.
-- ===========================================================================

create table public.agent_campaign_judge_runs (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    campaign_id uuid not null references public.agent_campaigns (id) on delete restrict,
    -- El tope cuenta por clave: una version nueva de la misma campana no lo reinicia (D-49.2).
    campaign_key text not null check (campaign_key ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    fingerprint text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
    status text not null check (status in ('corriendo', 'limpio', 'con_preguntas', 'error')),
    requested_by_kind text not null check (requested_by_kind = 'automation_expert'),
    requested_by_ref text not null,
    -- Lo que el AE le cuenta al duenio: veredicto, conteos y hasta diez preguntas recortadas.
    result jsonb check (result is null or (jsonb_typeof(result) = 'object' and octet_length(result::text) <= 16384)),
    error_code text check (error_code is null or error_code ~ '^[a-z][a-z0-9_:]{0,79}$'),
    started_at timestamptz not null default clock_timestamp(),
    finished_at timestamptz,
    constraint agent_campaign_judge_runs_requested_by
        check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    constraint agent_campaign_judge_runs_state_coherent check (coalesce(case status
        when 'corriendo' then finished_at is null and result is null and error_code is null
        when 'error' then finished_at is not null and error_code is not null
        else finished_at is not null and result is not null and error_code is null
    end, false))
);

-- **Una sola corriendo por instancia**: el juez tarda hasta 17 minutos y cada una cuesta.
create unique index agent_campaign_judge_runs_one_running
    on public.agent_campaign_judge_runs (tenant_ref, ally_ref) where status = 'corriendo';
create index agent_campaign_judge_runs_key_time_idx
    on public.agent_campaign_judge_runs (tenant_ref, ally_ref, campaign_key, started_at);
create index agent_campaign_judge_runs_campaign_time_idx
    on public.agent_campaign_judge_runs (campaign_id, started_at desc);

alter table public.agent_campaign_judge_runs enable row level security;

-- ===========================================================================
-- 2. Las funciones del bridge.
-- ===========================================================================

create function public.start_agent_campaign_judge_run_v1(
    p_instance jsonb, p_campaign_id uuid, p_requested_by_ref text, p_day_start timestamptz, p_daily_limit integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_campaign public.agent_campaigns%rowtype;
    v_now timestamptz := clock_timestamp();
    v_today integer;
    v_run uuid;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'agent_campaign_judge_requested_by_invalid';
    end if;
    if p_day_start is null or p_day_start > v_now or p_day_start <= v_now - interval '26 hours' then
        raise exception using errcode = '22023', message = 'agent_campaign_judge_day_start_invalid';
    end if;
    if p_daily_limit is null or p_daily_limit not between 1 and 10 then
        raise exception using errcode = '22023', message = 'agent_campaign_judge_daily_limit_invalid';
    end if;
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    if public._agent_campaign_expire_if_due(v_campaign) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_expired');
    end if;
    if v_campaign.status <> 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_validated',
            'status', v_campaign.status);
    end if;
    -- El juez tarda hasta 17 minutos: con menos de 20 la campana vence en el medio.
    if v_campaign.expires_at <= v_now + interval '20 minutes' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_judge_expires_soon',
            'expires_at', v_campaign.expires_at);
    end if;
    -- Una corriendo hace mas de 30 minutos ya no termina (el bridge se reinicio): queda como error.
    update public.agent_campaign_judge_runs r
       set status = 'error', error_code = 'agent_campaign_judge_abandoned', finished_at = v_now
     where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and r.status = 'corriendo' and r.started_at < v_now - interval '30 minutes';
    if exists (select 1 from public.agent_campaign_judge_runs r
                where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
                  and r.status = 'corriendo') then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_judge_running');
    end if;
    select count(*) into v_today from public.agent_campaign_judge_runs r
     where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and r.campaign_key = v_campaign.campaign_key and r.started_at >= p_day_start;
    if v_today >= p_daily_limit then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_judge_daily_limit',
            'runs_today', v_today, 'daily_limit', p_daily_limit);
    end if;
    begin
        insert into public.agent_campaign_judge_runs (
            tenant_ref, ally_ref, campaign_id, campaign_key, fingerprint, status,
            requested_by_kind, requested_by_ref, started_at)
        values (v_instance.o_tenant_ref, v_instance.o_ally_ref, v_campaign.id, v_campaign.campaign_key,
                v_campaign.fingerprint, 'corriendo', 'automation_expert', p_requested_by_ref, v_now)
        returning id into v_run;
    exception when unique_violation then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_judge_running');
    end;
    return jsonb_build_object('ok', true, 'run_id', v_run, 'runs_today', v_today + 1,
        'daily_limit', p_daily_limit, 'started_at', v_now);
end;
$function$;

create function public.finish_agent_campaign_judge_run_v1(
    p_instance jsonb, p_run_id uuid, p_status text, p_result jsonb, p_error_code text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_status text;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_status is null or p_status not in ('limpio', 'con_preguntas', 'error')
       or (p_status = 'error') <> (p_error_code is not null)
       or (p_status <> 'error' and p_result is null)
       or (p_error_code is not null and not coalesce(p_error_code ~ '^[a-z][a-z0-9_:]{0,79}$', false))
       or (p_result is not null and not coalesce(jsonb_typeof(p_result) = 'object'
           and octet_length(p_result::text) <= 16384, false)) then
        raise exception using errcode = '22023', message = 'agent_campaign_judge_result_invalid';
    end if;
    select r.status into v_status from public.agent_campaign_judge_runs r
     where r.id = p_run_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_judge_run_not_found';
    end if;
    if v_status <> 'corriendo' then
        -- Ya se dio por abandonada: el resultado tardio no la pisa.
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_judge_run_not_running',
            'status', v_status);
    end if;
    update public.agent_campaign_judge_runs
       set status = p_status, result = p_result, error_code = p_error_code, finished_at = clock_timestamp()
     where id = p_run_id;
    return jsonb_build_object('ok', true, 'status', p_status);
end;
$function$;

create function public.get_agent_campaign_judge_run_v1(p_instance jsonb, p_campaign_id uuid, p_day_start timestamptz)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_key text;
    v_now timestamptz := clock_timestamp();
    v_last jsonb;
    v_today integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_day_start is null or p_day_start > v_now or p_day_start <= v_now - interval '26 hours' then
        raise exception using errcode = '22023', message = 'agent_campaign_judge_day_start_invalid';
    end if;
    select c.campaign_key into v_key from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    -- La que sigue corriendo hace mas de 30 minutos se muestra como abandonada (esta funcion no escribe).
    select jsonb_build_object(
               'run_id', r.id,
               'status', case when r.status = 'corriendo' and r.started_at < v_now - interval '30 minutes'
                              then 'error' else r.status end,
               'error_code', case when r.status = 'corriendo' and r.started_at < v_now - interval '30 minutes'
                                  then 'agent_campaign_judge_abandoned' else r.error_code end,
               'result', r.result, 'started_at', r.started_at, 'finished_at', r.finished_at)
      into v_last
      from public.agent_campaign_judge_runs r
     where r.campaign_id = p_campaign_id and r.tenant_ref = v_instance.o_tenant_ref
       and r.ally_ref = v_instance.o_ally_ref
     order by r.started_at desc, r.id desc
     limit 1;
    select count(*) into v_today from public.agent_campaign_judge_runs r
     where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and r.campaign_key = v_key and r.started_at >= p_day_start;
    return jsonb_build_object('last_run', v_last, 'runs_today', v_today, 'database_at', v_now);
end;
$function$;

-- ===========================================================================
-- 3. Los permisos. La tabla, para nadie de la API. Las tres, solo service_role.
-- ===========================================================================

do $roles$
declare
    v_function regprocedure;
    v_role text;
begin
    revoke all on table public.agent_campaign_judge_runs from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role') order by 1
    loop
        execute format('revoke all on table public.agent_campaign_judge_runs from %I', v_role);
    end loop;
    foreach v_function in array array[
        'public.start_agent_campaign_judge_run_v1(jsonb,uuid,text,timestamptz,integer)',
        'public.finish_agent_campaign_judge_run_v1(jsonb,uuid,text,jsonb,text)',
        'public.get_agent_campaign_judge_run_v1(jsonb,uuid,timestamptz)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role', 'automation_expert_change',
                                 'flow_change_approver') order by 1
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.start_agent_campaign_judge_run_v1(jsonb, uuid, text, timestamptz, integer)
            to service_role;
        grant execute on function public.finish_agent_campaign_judge_run_v1(jsonb, uuid, text, jsonb, text)
            to service_role;
        grant execute on function public.get_agent_campaign_judge_run_v1(jsonb, uuid, timestamptz) to service_role;
    end if;
end;
$roles$;

commit;
