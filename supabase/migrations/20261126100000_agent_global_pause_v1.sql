-- agent-global-pause-v1: pausar al agente comercial para todas las conversaciones
-- de la instancia, para las de una etiqueta de Chatwoot o para las de un
-- producto (item 5.4 de la lista definitiva; D4 y D10 del mapa Z1 del
-- 2026-11-26). Contrato: docs/contracts/agent-global-pause-v1.md.
--
-- El Automation Expert la pone y la saca a pedido de una persona de la lista
-- de aprobadores (eso lo chequea el bridge con FLOW_APPROVAL_SLACK_APPROVERS:
-- la base no conoce la lista). Frenar no manda nada. La fila guarda quien la
-- pidio, el motivo, quien la saco y cuando. Mientras esta viva, el bridge no
-- llama al agente en las conversaciones que alcanza y anota cada una que dejo
-- para el equipo; al sacarla no se contesta para atras.
--
-- 1. Dos tablas nuevas, vacias: agent_global_pauses (una fila por pausa) y
--    agent_global_pause_holds (una fila por pausa y conversacion retenida).
--    Sin permisos para nadie de la API, con RLS.
-- 2. Tres funciones del Automation Expert, solo para automation_expert_change
--    (20261021000100): poner, sacar y listar.
-- 3. Dos para service_role: la lectura de las pausas que hace el turno (con
--    cache corta: las vivas y las que terminaron en las ultimas 48 horas, para
--    que un mensaje escrito durante una pausa no se conteste despues, cuando
--    el monitor de conversaciones trabadas lo vuelve a admitir) y el registro
--    de una conversacion retenida.
--
-- Una sola pausa viva por alcance y valor (un indice unico parcial): pedir la
-- misma otra vez contesta la que ya esta (replayed). Una con `hasta` vencido
-- se da por levantada (por vencimiento) en la siguiente escritura, y la
-- lectura del turno ya no la devuelve.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- AGENT_GLOBAL_PAUSE_ENABLED el bridge no llama a ninguna de estas funciones.
-- Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe o no es de esta instancia.
-- Los resultados del ciclo (tope de pausas vivas) vuelven como
-- {"ok": false, "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_store_actor_is_valid(text,text)') is null
       or to_regprocedure('public._flow_store_free_text_is_valid(text)') is null then
        raise exception using errcode = '55000', message = 'agent_global_pause_prerequisite_missing';
    end if;
    if to_regclass('public.agent_global_pauses') is not null then
        raise exception using errcode = '55000', message = 'agent_global_pause_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. Las tablas.
-- ===========================================================================

create table public.agent_global_pauses (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    -- todas: toda conversacion; etiqueta: las que tienen la etiqueta; producto:
    -- las del producto (su clave del manifiesto), por su etiqueta de producto.
    scope text not null check (scope in ('todas', 'etiqueta', 'producto')),
    scope_value text check (scope_value is null or scope_value ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    -- La etiqueta de Chatwoot que el turno compara (la del producto, resuelta
    -- por el bridge con el manifiesto al pausar).
    match_label text check (match_label is null or match_label ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    reason text not null check (public._flow_store_free_text_is_valid(reason)),
    requested_by_kind text not null check (requested_by_kind = 'automation_expert'),
    requested_by_ref text not null,
    created_at timestamptz not null default now(),
    until timestamptz,
    status text not null default 'activa' check (status in ('activa', 'levantada')),
    lifted_at timestamptz,
    lifted_by_kind text check (lifted_by_kind is null or lifted_by_kind in ('automation_expert', 'vencimiento')),
    lifted_by_ref text,
    lift_reason text check (lift_reason is null or public._flow_store_free_text_is_valid(lift_reason)),
    check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    check ((scope = 'todas') = (scope_value is null and match_label is null)),
    check (scope <> 'etiqueta' or (scope_value is not null and match_label = scope_value)),
    check (scope <> 'producto' or (scope_value is not null and match_label is not null)),
    check (until is null or (until > created_at and until <= created_at + interval '7 days')),
    check ((status = 'activa') = (lifted_at is null and lifted_by_kind is null)),
    check (lifted_by_kind is distinct from 'automation_expert'
           or public._flow_store_actor_is_valid('automation_expert', lifted_by_ref)),
    check (lifted_by_kind is distinct from 'vencimiento' or (lifted_by_ref is null and lift_reason is null)),
    check (lifted_by_kind is not null or (lifted_by_ref is null and lift_reason is null))
);

create unique index agent_global_pauses_one_live_per_scope
    on public.agent_global_pauses (tenant_ref, ally_ref, scope, coalesce(scope_value, ''))
    where status = 'activa';

create index agent_global_pauses_by_instance
    on public.agent_global_pauses (tenant_ref, ally_ref, created_at desc);

create table public.agent_global_pause_holds (
    pause_id uuid not null references public.agent_global_pauses (id),
    conversation_id bigint not null check (conversation_id > 0),
    first_held_at timestamptz not null default now(),
    last_held_at timestamptz not null default now(),
    held_messages integer not null default 1 check (held_messages > 0),
    primary key (pause_id, conversation_id),
    check (last_held_at >= first_held_at)
);

alter table public.agent_global_pauses enable row level security;
alter table public.agent_global_pause_holds enable row level security;

-- Una pausa no se borra y no cambia, salvo levantarla una vez. Lo retenido no
-- se borra.
create function public.reject_agent_global_pause_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000', message = 'agent_global_pause_is_permanent';
    end if;
    if tg_table_name = 'agent_global_pauses' then
        if old.status <> 'activa' or new.status <> 'levantada'
           or (new.id, new.tenant_ref, new.ally_ref, new.scope, new.scope_value, new.match_label, new.reason,
               new.requested_by_kind, new.requested_by_ref, new.created_at, new.until)
              is distinct from
              (old.id, old.tenant_ref, old.ally_ref, old.scope, old.scope_value, old.match_label, old.reason,
               old.requested_by_kind, old.requested_by_ref, old.created_at, old.until) then
            raise exception using errcode = '55000', message = 'agent_global_pause_is_permanent';
        end if;
    elsif (new.pause_id, new.conversation_id, new.first_held_at) is distinct from
          (old.pause_id, old.conversation_id, old.first_held_at)
          or new.held_messages < old.held_messages then
        raise exception using errcode = '55000', message = 'agent_global_pause_is_permanent';
    end if;
    return new;
end;
$function$;

create trigger agent_global_pauses_permanent
    before update or delete on public.agent_global_pauses
    for each row execute function public.reject_agent_global_pause_mutation();

create trigger agent_global_pause_holds_permanent
    before update or delete on public.agent_global_pause_holds
    for each row execute function public.reject_agent_global_pause_mutation();

-- ===========================================================================
-- 2. Lo interno.
-- ===========================================================================

-- La pausa como la ve quien llama: el estado efectivo (una activa con `hasta`
-- vencido es vencida), las conversaciones retenidas y quien la pidio.
create function public._agent_global_pause_view(p_pause public.agent_global_pauses, p_now timestamptz)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'pause_id', p_pause.id,
        'scope', p_pause.scope,
        'scope_value', p_pause.scope_value,
        'match_label', p_pause.match_label,
        'reason', p_pause.reason,
        'requested_by', p_pause.requested_by_ref,
        'created_at', p_pause.created_at,
        'until', p_pause.until,
        'status', case
            when p_pause.status = 'activa' and p_pause.until is not null and p_pause.until <= p_now then 'vencida'
            when p_pause.status = 'levantada' and p_pause.lifted_by_kind = 'vencimiento' then 'vencida'
            else p_pause.status end,
        'lifted_at', p_pause.lifted_at,
        'lifted_by', p_pause.lifted_by_ref,
        'lift_reason', p_pause.lift_reason,
        'held_conversations', (select count(*) from public.agent_global_pause_holds h where h.pause_id = p_pause.id),
        'held_messages', (select coalesce(sum(h.held_messages), 0) from public.agent_global_pause_holds h
                           where h.pause_id = p_pause.id));
$function$;

-- Las activas de la instancia con `hasta` vencido quedan levantadas por
-- vencimiento (en la hora en que vencieron).
create function public._agent_global_pauses_expire_due(p_tenant_ref text, p_ally_ref text, p_now timestamptz)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    update public.agent_global_pauses p
       set status = 'levantada', lifted_at = p.until, lifted_by_kind = 'vencimiento'
     where p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref and p.status = 'activa'
       and p.until is not null and p.until <= p_now;
$function$;

-- ===========================================================================
-- 3. Las funciones del Automation Expert.
-- ===========================================================================

-- Poner una pausa. La misma (alcance y valor) ya viva contesta esa, sin
-- cambiarla. Hasta 20 vivas por instancia.
create function public.pause_agent_globally_v1(
    p_instance jsonb, p_scope text, p_scope_value text, p_match_label text, p_until timestamptz,
    p_reason text, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_now timestamptz := now();
    v_pause public.agent_global_pauses%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if p_scope is null or p_scope not in ('todas', 'etiqueta', 'producto')
       or (p_scope = 'todas') <> (p_scope_value is null and p_match_label is null)
       or (p_scope = 'etiqueta' and (p_scope_value is null or p_match_label is distinct from p_scope_value))
       or (p_scope = 'producto' and (p_scope_value is null or p_match_label is null))
       or not coalesce(p_scope_value is null or p_scope_value ~ '^[a-z0-9][a-z0-9_-]{0,63}$', false)
       or not coalesce(p_match_label is null or p_match_label ~ '^[a-z0-9][a-z0-9_-]{0,63}$', false) then
        raise exception using errcode = '22023', message = 'agent_pause_scope_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'agent_pause_reason_invalid';
    end if;
    if p_until is not null and (p_until <= v_now + interval '1 minute' or p_until > v_now + interval '7 days') then
        raise exception using errcode = '22023', message = 'agent_pause_until_invalid';
    end if;
    -- Una por instancia a la vez: el tope y el indice unico se miran juntos.
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_global_pause:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    perform public._agent_global_pauses_expire_due(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_now);

    select * into v_pause from public.agent_global_pauses p
     where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
       and p.status = 'activa' and p.scope = p_scope and coalesce(p.scope_value, '') = coalesce(p_scope_value, '');
    if found then
        return jsonb_build_object('ok', true, 'replayed', true,
            'pause', public._agent_global_pause_view(v_pause, v_now));
    end if;
    if (select count(*) from public.agent_global_pauses p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and p.status = 'activa') >= 20 then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_pause_open_limit');
    end if;
    insert into public.agent_global_pauses (tenant_ref, ally_ref, scope, scope_value, match_label, reason,
                                            requested_by_kind, requested_by_ref, created_at, until)
    values (v_instance.o_tenant_ref, v_instance.o_ally_ref, p_scope, p_scope_value, p_match_label, btrim(p_reason),
            'automation_expert', p_requested_by_ref, v_now, p_until)
    returning * into v_pause;
    return jsonb_build_object('ok', true, 'replayed', false,
        'pause', public._agent_global_pause_view(v_pause, v_now));
end;
$function$;

-- Sacar una pausa. Ya levantada (o vencida) contesta replayed.
create function public.resume_agent_globally_v1(
    p_instance jsonb, p_pause_id uuid, p_reason text, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_now timestamptz := now();
    v_pause public.agent_global_pauses%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if p_reason is not null and not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'agent_pause_reason_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_global_pause:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    perform public._agent_global_pauses_expire_due(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_now);
    select * into v_pause from public.agent_global_pauses p
     where p.id = p_pause_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_pause_not_found';
    end if;
    if v_pause.status <> 'activa' then
        return jsonb_build_object('ok', true, 'replayed', true,
            'pause', public._agent_global_pause_view(v_pause, v_now));
    end if;
    update public.agent_global_pauses
       set status = 'levantada', lifted_at = v_now, lifted_by_kind = 'automation_expert',
           lifted_by_ref = p_requested_by_ref, lift_reason = nullif(btrim(coalesce(p_reason, '')), '')
     where id = v_pause.id
    returning * into v_pause;
    return jsonb_build_object('ok', true, 'replayed', false,
        'pause', public._agent_global_pause_view(v_pause, v_now));
end;
$function$;

-- Las pausas de la instancia: las vivas primero y despues las ultimas
-- levantadas, hasta p_limit (1 a 50). Solo lee.
create function public.list_agent_global_pauses_v1(p_instance jsonb, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_now timestamptz := now();
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'agent_pause_limit_invalid';
    end if;
    return jsonb_build_object('pauses', coalesce((
        select jsonb_agg(public._agent_global_pause_view(p, v_now) order by
                   (p.status = 'activa' and (p.until is null or p.until > v_now)) desc, p.created_at desc)
          from (select * from public.agent_global_pauses q
                 where q.tenant_ref = v_instance.o_tenant_ref and q.ally_ref = v_instance.o_ally_ref
                 order by (q.status = 'activa' and (q.until is null or q.until > v_now)) desc, q.created_at desc
                 limit p_limit) p), '[]'::jsonb));
end;
$function$;

-- ===========================================================================
-- 4. Las de service_role (el turno del bridge).
-- ===========================================================================

-- Las pausas de la instancia que el turno necesita, sin quien la pidio ni el
-- motivo: las vivas y las que terminaron (levantadas o vencidas) en las
-- ultimas 48 horas, con su fin (ends_at, null si sigue sin fin). Un mensaje
-- escrito entre created_at y ends_at no lo contesta el agente, aunque llegue
-- al turno despues.
create function public.get_live_agent_global_pauses_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_now timestamptz := now();
begin
    v_instance := public._flow_store_instance(p_instance);
    return jsonb_build_object('pauses', coalesce((
        select jsonb_agg(jsonb_build_object(
                   'pause_id', e.id, 'scope', e.scope, 'scope_value', e.scope_value,
                   'match_label', e.match_label, 'created_at', e.created_at, 'ends_at', e.ends_at)
                   order by e.created_at)
          from (select p.*, case when p.lifted_at is not null and p.until is not null then least(p.lifted_at, p.until)
                                 else coalesce(p.lifted_at, p.until) end as ends_at
                  from public.agent_global_pauses p
                 where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref) e
         where e.ends_at is null or e.ends_at > v_now - interval '48 hours'), '[]'::jsonb));
end;
$function$;

-- Una conversacion que el turno dejo para el equipo por una pausa. La primera
-- vez la registra; las siguientes suman el mensaje. Una pausa que ya no esta
-- viva contesta not_active y no anota nada.
create function public.record_agent_global_pause_hold_v1(
    p_instance jsonb, p_pause_id uuid, p_conversation_id bigint)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_now timestamptz := now();
    v_inserted boolean;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_pause_id is null or p_conversation_id is null or p_conversation_id < 1 then
        raise exception using errcode = '22023', message = 'agent_pause_hold_invalid';
    end if;
    perform 1 from public.agent_global_pauses p
     where p.id = p_pause_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
       and p.status = 'activa' and (p.until is null or p.until > v_now);
    if not found then
        return jsonb_build_object('ok', true, 'outcome', 'not_active');
    end if;
    insert into public.agent_global_pause_holds (pause_id, conversation_id, first_held_at, last_held_at)
    values (p_pause_id, p_conversation_id, v_now, v_now)
    on conflict (pause_id, conversation_id) do update
       set last_held_at = greatest(public.agent_global_pause_holds.last_held_at, excluded.last_held_at),
           held_messages = public.agent_global_pause_holds.held_messages + 1
    returning (xmax = 0) into v_inserted;
    return jsonb_build_object('ok', true, 'outcome', case when v_inserted then 'recorded' else 'repeated' end);
end;
$function$;

-- ===========================================================================
-- 5. Los permisos. Las tablas, para nadie de la API. Las internas y el
--    disparador, para nadie. Cada funcion publica, solo para su rol.
-- ===========================================================================

do $acl$
declare
    v_table text;
    v_function regprocedure;
    v_role text;
begin
    foreach v_table in array array['agent_global_pauses', 'agent_global_pause_holds'] loop
        execute format('revoke all on table public.%I from public', v_table);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on table public.%I from %I', v_table, v_role);
        end loop;
    end loop;
    foreach v_function in array array[
        'public.reject_agent_global_pause_mutation()',
        'public._agent_global_pause_view(public.agent_global_pauses,timestamptz)',
        'public._agent_global_pauses_expire_due(text,text,timestamptz)',
        'public.pause_agent_globally_v1(jsonb,text,text,text,timestamptz,text,text)',
        'public.resume_agent_globally_v1(jsonb,uuid,text,text)',
        'public.list_agent_global_pauses_v1(jsonb,integer)',
        'public.get_live_agent_global_pauses_v1(jsonb)',
        'public.record_agent_global_pause_hold_v1(jsonb,uuid,bigint)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    -- El rol lo crea 20261021000100; una base sin el no lo nombra.
    if to_regrole('automation_expert_change') is not null then
        foreach v_function in array array[
            'public.pause_agent_globally_v1(jsonb,text,text,text,timestamptz,text,text)',
            'public.resume_agent_globally_v1(jsonb,uuid,text,text)',
            'public.list_agent_global_pauses_v1(jsonb,integer)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to automation_expert_change', v_function);
        end loop;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.get_live_agent_global_pauses_v1(jsonb) to service_role;
        grant execute on function public.record_agent_global_pause_hold_v1(jsonb, uuid, bigint) to service_role;
    end if;
end;
$acl$;

commit;
