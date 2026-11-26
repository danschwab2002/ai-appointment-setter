-- agent-temporary-instruction-v1: la instruccion temporal para todas las
-- conversaciones (5.3 de la serie Z1, 2026-11-26). Contrato:
-- docs/contracts/agent-temporary-instruction-v1.md.
--
-- El AE redacta con el duenio un texto corto («hoy no ofrezcas cupon», «avisa
-- que el vivo se movio») con un vencimiento obligatorio. El bridge corre las
-- reglas (links, telefonos, mails, montos) antes de guardar, asi que proponer
-- la deja ya congelada con su huella por 24 horas: no hay borrador ni juez.
-- Una persona de la lista de aprobadores, desde la pagina de aprobacion, la
-- publica: sin ese si no se sirve. Publicada, el bridge la lee (con cache
-- corta) y la manda como `system` en todos los turnos del agente hasta su
-- `hasta`. Se retira desde la misma pagina.
--
-- 1. Dos tablas nuevas, vacias: agent_temporary_instructions (una fila por
--    version) y agent_temporary_instruction_events (solo agregar). Sin
--    permisos para nadie de la API, con RLS.
-- 2. Tres funciones del Automation Expert, solo para automation_expert_change
--    (20261021000100): proponer, releer y listar. Ni publicar, ni retirar, ni
--    descartar.
-- 3. Cuatro de la pagina, solo para flow_change_approver (20261028000100),
--    con la sesion viva: el actor slack sale de la fila de la sesion, nunca de
--    un parametro. Releer, publicar, retirar y descartar.
-- 4. Una para service_role: la lectura de la viva.
--
-- Una sola publicada por instancia (D11): un indice unico parcial. Publicar
-- otra con una publicada la retira (sistema/reemplazada, D-53.3). Proponer
-- otra descarta la que esperaba el si (sistema/reemplazada, D-53.2).
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- AGENT_TEMPORARY_INSTRUCTION_ENABLED el bridge no llama a ninguna de estas
-- funciones. Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 28000 sesion invalida
-- (flow_approval_session_invalid); P0002 no existe o no es de esta instancia.
-- Los resultados del ciclo (vencida, sin vigencia, tope...) vuelven como
-- {"ok": false, "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_store_actor_is_valid(text,text)') is null
       or to_regprocedure('public._flow_store_free_text_is_valid(text)') is null
       or to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regprocedure('public.set_updated_at()') is null then
        raise exception using errcode = '55000', message = 'agent_temporary_instructions_prerequisite_missing';
    end if;
    if to_regclass('public.agent_temporary_instructions') is not null then
        raise exception using errcode = '55000', message = 'agent_temporary_instructions_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. La forma del texto.
-- ===========================================================================

-- Hasta 600 caracteres, con saltos de linea, sin los controles ni los
-- invisibles que rechaza el texto libre del almacen (20261016000100). Las
-- reglas de negocio (links, telefonos, mails, montos) las corre el bridge.
create function public._agent_temporary_instruction_text_is_valid(p_text text)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(
        length(btrim(p_text)) between 1 and 600
        and length(p_text) <= 600
        and p_text !~ '[\u0001-\u0009\u000b-\u001f\u007f-\u009f­؜᠎​-‏ -‮⁠-⁤⁦-⁯﻿￹-￻\U000e0000-\U000e007f]',
        false)
$function$;

-- ===========================================================================
-- 2. Las tablas.
-- ===========================================================================

create table public.agent_temporary_instructions (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    status text not null check (status in ('validada', 'publicada', 'retirada', 'vencida', 'descartada')),
    -- El numero de la instruccion en la instancia: va en la procedencia de cada turno.
    version integer not null check (version > 0),
    instruction_text text not null check (public._agent_temporary_instruction_text_is_valid(instruction_text)),
    purpose text not null check (public._flow_store_free_text_is_valid(purpose)),
    valid_until timestamptz not null,
    requested_by_kind text not null check (requested_by_kind = 'automation_expert'),
    requested_by_ref text not null,
    validator jsonb not null check (jsonb_typeof(validator) = 'object' and octet_length(validator::text) <= 1024),
    fingerprint text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
    expires_at timestamptz not null,
    approved_at timestamptz,
    approved_by_kind text check (approved_by_kind = 'slack'),
    approved_by_ref text,
    closed_at timestamptz,
    closed_by_kind text check (closed_by_kind in ('slack', 'sistema')),
    closed_by_ref text,
    close_reason text check (close_reason is null or public._flow_store_free_text_is_valid(close_reason)),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    -- Como mucho 7 dias desde que se propuso, y despues de proponerla.
    constraint agent_temporary_instructions_window
        check (valid_until > created_at and valid_until <= created_at + interval '7 days'),
    constraint agent_temporary_instructions_requested_by
        check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    constraint agent_temporary_instructions_approved_by
        check ((approved_by_kind is null and approved_by_ref is null)
               or public._flow_store_actor_is_valid(approved_by_kind, approved_by_ref)),
    constraint agent_temporary_instructions_closed_by
        check ((closed_by_kind is null and closed_by_ref is null)
               or public._flow_store_actor_is_valid(closed_by_kind, closed_by_ref)),
    -- El CASE entero adentro de coalesce: una comparacion con NULL no deja pasar la fila.
    constraint agent_temporary_instructions_state_coherent check (coalesce(case status
        when 'validada' then expires_at > created_at and approved_at is null and closed_at is null
        when 'publicada' then approved_at is not null and approved_by_kind is not null and closed_at is null
        when 'retirada' then approved_at is not null and closed_at is not null and closed_by_kind is not null
            and close_reason is not null
        when 'vencida' then approved_at is null and closed_at is not null and closed_by_kind = 'sistema'
        when 'descartada' then approved_at is null and closed_at is not null and closed_by_kind is not null
            and close_reason is not null
    end, false))
);

-- El id es un uuid y no un identity: una secuencia recibiria permisos por los
-- privilegios por defecto de Supabase.
create table public.agent_temporary_instruction_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    instruction_id uuid not null references public.agent_temporary_instructions (id) on delete restrict,
    event_type text not null check (event_type in (
        'agent_temporary_instruction_proposed', 'agent_temporary_instruction_published',
        'agent_temporary_instruction_publish_refused', 'agent_temporary_instruction_retired',
        'agent_temporary_instruction_discarded', 'agent_temporary_instruction_expired')),
    actor_kind text not null check (actor_kind in ('automation_expert', 'slack', 'sistema')),
    actor_ref text not null,
    reason_code text not null check (reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'),
    data jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object'
        and octet_length(data::text) <= 4096),
    created_at timestamptz not null default clock_timestamp(),
    constraint agent_temporary_instruction_events_actor
        check (public._flow_store_actor_is_valid(actor_kind, actor_ref))
);

-- **Una sola publicada por instancia.** Aunque el bridge lo mire antes, esto es lo que lo hace cumplir.
create unique index agent_temporary_instructions_one_live
    on public.agent_temporary_instructions (tenant_ref, ally_ref) where status = 'publicada';
-- Un numero nombra una sola instruccion de la instancia.
create unique index agent_temporary_instructions_version_once
    on public.agent_temporary_instructions (tenant_ref, ally_ref, version);
create index agent_temporary_instructions_instance_time_idx
    on public.agent_temporary_instructions (tenant_ref, ally_ref, created_at desc);
create index agent_temporary_instruction_events_instruction_time_idx
    on public.agent_temporary_instruction_events (instruction_id, created_at);

alter table public.agent_temporary_instructions enable row level security;
alter table public.agent_temporary_instruction_events enable row level security;

-- ===========================================================================
-- 3. Los disparadores.
-- ===========================================================================

create function public.reject_agent_temporary_instruction_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'agent_temporary_instruction_events' then 'agent_temporary_instruction_event_is_append_only'
        else 'agent_temporary_instruction_is_permanent'
    end;
end;
$function$;

-- El recorrido. El contenido nace congelado; desde publicada, tambien la
-- aprobacion; un estado final no acepta ningun UPDATE.
create function public.validate_agent_temporary_instruction_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if old.status in ('retirada', 'vencida', 'descartada') then
        raise exception using errcode = '55000', message = 'agent_temporary_instruction_frozen';
    end if;
    if row(new.id, new.tenant_ref, new.ally_ref, new.version, new.instruction_text, new.purpose, new.valid_until,
           new.requested_by_kind, new.requested_by_ref, new.validator, new.fingerprint, new.expires_at,
           new.created_at)
       is distinct from row(old.id, old.tenant_ref, old.ally_ref, old.version, old.instruction_text, old.purpose,
           old.valid_until, old.requested_by_kind, old.requested_by_ref, old.validator, old.fingerprint,
           old.expires_at, old.created_at) then
        raise exception using errcode = '55000', message = 'agent_temporary_instruction_frozen';
    end if;
    if new.status is distinct from old.status and not (
        (old.status = 'validada' and new.status in ('publicada', 'vencida', 'descartada'))
        or (old.status = 'publicada' and new.status = 'retirada')
    ) then
        raise exception using errcode = '23514', message = 'agent_temporary_instruction_transition_invalid';
    end if;
    if old.status = 'publicada'
       and row(new.approved_at, new.approved_by_kind, new.approved_by_ref)
           is distinct from row(old.approved_at, old.approved_by_kind, old.approved_by_ref) then
        raise exception using errcode = '55000', message = 'agent_temporary_instruction_frozen';
    end if;
    return new;
end;
$function$;

create trigger agent_temporary_instructions_validate
before update on public.agent_temporary_instructions
for each row execute function public.validate_agent_temporary_instruction_change();
create trigger agent_temporary_instructions_permanent
before delete on public.agent_temporary_instructions
for each row execute function public.reject_agent_temporary_instruction_mutation();
create trigger agent_temporary_instructions_no_truncate
before truncate on public.agent_temporary_instructions
for each statement execute function public.reject_agent_temporary_instruction_mutation();
create trigger agent_temporary_instructions_set_updated_at
before update on public.agent_temporary_instructions
for each row execute function public.set_updated_at();
create trigger agent_temporary_instruction_events_append_only
before update or delete on public.agent_temporary_instruction_events
for each row execute function public.reject_agent_temporary_instruction_mutation();
create trigger agent_temporary_instruction_events_no_truncate
before truncate on public.agent_temporary_instruction_events
for each statement execute function public.reject_agent_temporary_instruction_mutation();

-- ===========================================================================
-- 4. Los helpers internos (security invoker, de nadie de la API).
-- ===========================================================================

create function public._agent_temporary_instruction_event(
    p_row public.agent_temporary_instructions, p_event_type text, p_actor_kind text, p_actor_ref text,
    p_reason_code text, p_data jsonb)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.agent_temporary_instruction_events (tenant_ref, ally_ref, instruction_id, event_type,
        actor_kind, actor_ref, reason_code, data)
    values (p_row.tenant_ref, p_row.ally_ref, p_row.id, p_event_type,
        p_actor_kind, p_actor_ref, p_reason_code, coalesce(p_data, '{}'::jsonb))
$function$;

-- Con la fila ya bloqueada: si esta validada y paso su ventana de aprobacion,
-- la pasa a vencida con sistema/vencimiento y deja su evento. No hay barrido.
create function public._agent_temporary_instruction_expire_if_due(p_row public.agent_temporary_instructions)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_row.status is distinct from 'validada' or not coalesce(clock_timestamp() >= p_row.expires_at, false) then
        return false;
    end if;
    update public.agent_temporary_instructions
       set status = 'vencida', closed_at = clock_timestamp(), closed_by_kind = 'sistema', closed_by_ref = 'vencimiento'
     where id = p_row.id;
    perform public._agent_temporary_instruction_event(p_row, 'agent_temporary_instruction_expired', 'sistema',
        'vencimiento', 'agent_temporary_instruction_expired', jsonb_build_object('expires_at', p_row.expires_at));
    return true;
end;
$function$;

-- Lo que se muestra. Con p_with_people (la pagina), el ID de Slack de quien la
-- aprobo y de quien la cerro; el agente no lo recibe. Nunca quien la pidio.
create function public._agent_temporary_instruction_view(
    p_row public.agent_temporary_instructions, p_with_people boolean)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'instruction_id', p_row.id,
        'status', p_row.status,
        'effective_status', case
            when p_row.status = 'validada' and clock_timestamp() >= p_row.expires_at then 'vencida'
            when p_row.status = 'publicada' and clock_timestamp() >= p_row.valid_until then 'terminada'
            else p_row.status end,
        'version', p_row.version,
        'texto', p_row.instruction_text,
        'purpose', p_row.purpose,
        'valid_until', p_row.valid_until,
        'fingerprint', p_row.fingerprint,
        'expires_at', p_row.expires_at,
        'approved_at', p_row.approved_at,
        'closed_at', p_row.closed_at,
        'closed_by_kind', p_row.closed_by_kind,
        'close_reason_code', case when p_row.closed_by_kind = 'sistema' then p_row.closed_by_ref end,
        'created_at', p_row.created_at,
        'events', coalesce((
            select jsonb_agg(jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                'reason_code', e.reason_code, 'created_at', e.created_at)
                             order by e.created_at desc, e.id)
              from (select * from public.agent_temporary_instruction_events e
                     where e.instruction_id = p_row.id
                     order by e.created_at desc, e.id limit 20) e), '[]'::jsonb))
        || case when p_with_people then jsonb_build_object('approved_by_ref', p_row.approved_by_ref,
                                                           'closed_by_ref', p_row.closed_by_ref)
                else '{}'::jsonb end
$function$;

-- La publicada y vigente de la instancia, si hay (a lo sumo una).
create function public._agent_temporary_instruction_live(p_tenant_ref text, p_ally_ref text)
returns public.agent_temporary_instructions
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select * from public.agent_temporary_instructions t
     where t.tenant_ref = p_tenant_ref and t.ally_ref = p_ally_ref
       and t.status = 'publicada' and t.valid_until > clock_timestamp()
     limit 1
$function$;

-- ===========================================================================
-- 5. Las tres del Automation Expert.
-- ===========================================================================

-- Proponer: la base le cree al bridge las reglas de negocio, pero mira la
-- forma, arma ella la huella y congela 24 horas para el si. Descarta la que
-- esperaba el si (a lo sumo una esperando). Tope: 20 propuestas por dia.
create function public.propose_agent_temporary_instruction_v1(
    p_instance jsonb, p_text text, p_valid_until timestamptz, p_purpose text, p_validator jsonb,
    p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_row public.agent_temporary_instructions%rowtype;
    v_old public.agent_temporary_instructions%rowtype;
    v_now timestamptz;
    v_today integer;
    v_version integer;
    v_id uuid;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_purpose) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    if not public._agent_temporary_instruction_text_is_valid(p_text) then
        raise exception using errcode = '22023', message = 'agent_temporary_instruction_text_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_validator) = 'object'
        and (select array_agg(k order by k) from jsonb_object_keys(p_validator) k)
            = array['contract', 'git_sha', 'product_version', 'rules_version']
        and p_validator ->> 'contract' = 'agent-temporary-instruction-v1'
        and jsonb_typeof(p_validator -> 'rules_version') = 'number'
        and p_validator ->> 'rules_version' ~ '^[1-9][0-9]{0,3}$'
        and p_validator ->> 'product_version' ~ '^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
        and p_validator ->> 'git_sha' ~ '^[0-9a-f]{7,40}$', false) then
        raise exception using errcode = '22023', message = 'agent_temporary_instruction_validator_invalid';
    end if;
    v_now := clock_timestamp();
    if p_valid_until is null or p_valid_until <= v_now then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_window_over');
    end if;
    if p_valid_until > v_now + interval '7 days' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_window_too_long');
    end if;

    -- Un lock por instancia: dos propuestas a la vez no toman el mismo numero ni pasan el tope.
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_temporary_instructions:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select count(*) into v_today from public.agent_temporary_instructions t
     where t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref
       and t.created_at > v_now - interval '24 hours';
    if v_today >= 20 then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_daily_limit');
    end if;
    for v_old in
        select * from public.agent_temporary_instructions t
         where t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref
           and t.status = 'validada'
         for update
    loop
        if not public._agent_temporary_instruction_expire_if_due(v_old) then
            update public.agent_temporary_instructions
               set status = 'descartada', closed_at = v_now, closed_by_kind = 'sistema',
                   closed_by_ref = 'reemplazada', close_reason = 'reemplazada por una propuesta nueva'
             where id = v_old.id;
            perform public._agent_temporary_instruction_event(v_old, 'agent_temporary_instruction_discarded',
                'sistema', 'reemplazada', 'agent_temporary_instruction_replaced', '{}'::jsonb);
        end if;
    end loop;
    select coalesce(max(t.version), 0) + 1 into v_version from public.agent_temporary_instructions t
     where t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref;
    v_id := gen_random_uuid();
    insert into public.agent_temporary_instructions (id, tenant_ref, ally_ref, status, version, instruction_text,
        purpose, valid_until, requested_by_kind, requested_by_ref, validator, fingerprint, expires_at, created_at)
    values (v_id, v_instance.o_tenant_ref, v_instance.o_ally_ref, 'validada', v_version, p_text, p_purpose,
        p_valid_until, 'automation_expert', p_requested_by_ref, p_validator,
        -- La huella la calcula solo la base, sobre lo que queda congelado.
        encode(sha256(convert_to(jsonb_build_object(
            'contract', 'agent-temporary-instruction-v1', 'instruction_id', v_id, 'version', v_version,
            'texto', p_text, 'valid_until', p_valid_until, 'purpose', p_purpose)::text, 'UTF8')), 'hex'),
        v_now + interval '24 hours', v_now)
    returning * into v_row;
    perform public._agent_temporary_instruction_event(v_row, 'agent_temporary_instruction_proposed',
        'automation_expert', p_requested_by_ref, 'agent_temporary_instruction_proposed', jsonb_build_object(
            'version', v_row.version, 'validator', p_validator, 'valid_until', v_row.valid_until,
            'text_sha256', encode(sha256(convert_to(p_text, 'UTF8')), 'hex'),
            'purpose_sha256', encode(sha256(convert_to(p_purpose, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'instruction_id', v_row.id, 'version', v_row.version,
        'status', v_row.status, 'fingerprint', v_row.fingerprint, 'expires_at', v_row.expires_at,
        'valid_until', v_row.valid_until);
end;
$function$;

-- Releer una, sin el ID de Slack de nadie.
create function public.get_agent_temporary_instruction_v1(p_instance jsonb, p_instruction_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_row public.agent_temporary_instructions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_row from public.agent_temporary_instructions t
     where t.id = p_instruction_id and t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_temporary_instruction_not_found';
    end if;
    return public._agent_temporary_instruction_view(v_row, false);
end;
$function$;

-- Las de la instancia, de la mas nueva a la mas vieja, hasta p_limit (1 a 50),
-- y cual es la viva ahora.
create function public.list_agent_temporary_instructions_v1(p_instance jsonb, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_rows jsonb;
    v_live public.agent_temporary_instructions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'agent_temporary_instruction_list_parameters_invalid';
    end if;
    select coalesce(jsonb_agg(row_data order by created_at desc, instruction_id), '[]'::jsonb)
      into v_rows
      from (
        select t.created_at, t.id as instruction_id, jsonb_build_object(
                   'instruction_id', t.id,
                   'status', t.status,
                   'effective_status', case
                       when t.status = 'validada' and clock_timestamp() >= t.expires_at then 'vencida'
                       when t.status = 'publicada' and clock_timestamp() >= t.valid_until then 'terminada'
                       else t.status end,
                   'version', t.version,
                   'texto', t.instruction_text,
                   'purpose', t.purpose,
                   'valid_until', t.valid_until,
                   'expires_at', t.expires_at,
                   'approved_at', t.approved_at,
                   'closed_at', t.closed_at,
                   'created_at', t.created_at) as row_data
          from public.agent_temporary_instructions t
         where t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref
         order by t.created_at desc, t.id
         limit p_limit) listed;
    v_live := public._agent_temporary_instruction_live(v_instance.o_tenant_ref, v_instance.o_ally_ref);
    return jsonb_build_object('instructions', v_rows, 'live_instruction_id', v_live.id,
        'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 6. Las cuatro de la pagina, con la sesion viva.
-- ===========================================================================

-- Releer, y si hay otra viva (lo que la pagina avisa antes del boton).
create function public.approver_get_agent_temporary_instruction_v1(
    p_instance jsonb, p_session_hash text, p_instruction_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_row public.agent_temporary_instructions%rowtype;
    v_live public.agent_temporary_instructions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_row from public.agent_temporary_instructions t
     where t.id = p_instruction_id and t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_temporary_instruction_not_found';
    end if;
    v_live := public._agent_temporary_instruction_live(v_instance.o_tenant_ref, v_instance.o_ally_ref);
    return public._agent_temporary_instruction_view(v_row, true) || jsonb_build_object(
        'other_live', case when v_live.id is not null and v_live.id <> v_row.id
            then jsonb_build_object('version', v_live.version, 'valid_until', v_live.valid_until) end);
end;
$function$;

-- Publicar: el si. La huella viaja con el si. Una publicada anterior se retira
-- (sistema/reemplazada): una sola viva.
create function public.approver_publish_agent_temporary_instruction_v1(
    p_instance jsonb, p_session_hash text, p_instruction_id uuid, p_fingerprint text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_row public.agent_temporary_instructions%rowtype;
    v_other public.agent_temporary_instructions%rowtype;
    v_now timestamptz;
    v_replaced uuid;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_temporary_instruction_fingerprint_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_temporary_instructions:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_row from public.agent_temporary_instructions t
     where t.id = p_instruction_id and t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_temporary_instruction_not_found';
    end if;
    if p_fingerprint is distinct from v_row.fingerprint then
        raise exception using errcode = '22023', message = 'agent_temporary_instruction_fingerprint_mismatch';
    end if;
    if public._agent_temporary_instruction_expire_if_due(v_row) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_expired');
    end if;
    if v_row.status = 'publicada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', 'publicada');
    end if;
    if v_row.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_not_validated',
            'status', v_row.status);
    end if;
    v_now := clock_timestamp();
    if v_row.valid_until <= v_now then
        perform public._agent_temporary_instruction_event(v_row, 'agent_temporary_instruction_publish_refused',
            'slack', v_actor, 'agent_temporary_instruction_window_over', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_window_over');
    end if;
    for v_other in
        select * from public.agent_temporary_instructions t
         where t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref
           and t.status = 'publicada' and t.id <> v_row.id
         for update
    loop
        update public.agent_temporary_instructions
           set status = 'retirada', closed_at = v_now, closed_by_kind = 'sistema', closed_by_ref = 'reemplazada',
               close_reason = 'reemplazada por otra instruccion publicada'
         where id = v_other.id;
        perform public._agent_temporary_instruction_event(v_other, 'agent_temporary_instruction_retired',
            'sistema', 'reemplazada', 'agent_temporary_instruction_replaced',
            jsonb_build_object('by_version', v_row.version));
        v_replaced := v_other.id;
    end loop;
    update public.agent_temporary_instructions
       set status = 'publicada', approved_at = v_now, approved_by_kind = 'slack', approved_by_ref = v_actor
     where id = v_row.id;
    perform public._agent_temporary_instruction_event(v_row, 'agent_temporary_instruction_published', 'slack',
        v_actor, 'agent_temporary_instruction_published', jsonb_build_object('fingerprint', v_row.fingerprint,
            'version', v_row.version, 'replaced', v_replaced is not null));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada',
        'replaced', v_replaced is not null);
end;
$function$;

-- Retirar: el agente deja de usarla en la proxima recarga. No borra nada.
create function public.approver_retire_agent_temporary_instruction_v1(
    p_instance jsonb, p_session_hash text, p_instruction_id uuid, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_row public.agent_temporary_instructions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_row from public.agent_temporary_instructions t
     where t.id = p_instruction_id and t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_temporary_instruction_not_found';
    end if;
    if v_row.status = 'retirada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', 'retirada');
    end if;
    if v_row.status is distinct from 'publicada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_not_live',
            'status', v_row.status);
    end if;
    update public.agent_temporary_instructions
       set status = 'retirada', closed_at = clock_timestamp(), closed_by_kind = 'slack', closed_by_ref = v_actor,
           close_reason = p_reason
     where id = v_row.id;
    perform public._agent_temporary_instruction_event(v_row, 'agent_temporary_instruction_retired', 'slack',
        v_actor, 'agent_temporary_instruction_retired', jsonb_build_object(
            'reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'retirada');
end;
$function$;

-- Descartar, con un motivo de una linea. Solo lo que nunca se publico.
create function public.approver_discard_agent_temporary_instruction_v1(
    p_instance jsonb, p_session_hash text, p_instruction_id uuid, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_row public.agent_temporary_instructions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_row from public.agent_temporary_instructions t
     where t.id = p_instruction_id and t.tenant_ref = v_instance.o_tenant_ref and t.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_temporary_instruction_not_found';
    end if;
    if public._agent_temporary_instruction_expire_if_due(v_row) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_final', 'status', 'vencida');
    end if;
    if v_row.status = 'descartada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_row.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_temporary_instruction_final',
            'status', v_row.status);
    end if;
    update public.agent_temporary_instructions
       set status = 'descartada', closed_at = clock_timestamp(), closed_by_kind = 'slack',
           closed_by_ref = v_actor, close_reason = p_reason
     where id = v_row.id;
    perform public._agent_temporary_instruction_event(v_row, 'agent_temporary_instruction_discarded', 'slack',
        v_actor, 'agent_temporary_instruction_discarded', jsonb_build_object(
            'close_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

-- ===========================================================================
-- 7. La de service_role: la viva.
-- ===========================================================================

-- Lo que el bridge sirve: la publicada y vigente de la instancia (a lo sumo
-- una), o null. Sin IDs de Slack. El bridge vuelve a mirar el `hasta` en cada turno.
create function public.get_live_agent_temporary_instruction_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_live public.agent_temporary_instructions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_live := public._agent_temporary_instruction_live(v_instance.o_tenant_ref, v_instance.o_ally_ref);
    return jsonb_build_object(
        'instruction', case when v_live.id is null then null else jsonb_build_object(
            'instruction_id', v_live.id,
            'version', v_live.version,
            'texto', v_live.instruction_text,
            'valid_until', v_live.valid_until,
            'fingerprint', v_live.fingerprint,
            'approved_at', v_live.approved_at) end,
        'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 8. Los permisos. Las tablas, para nadie de la API. Las internas y los
--    disparadores, para nadie. Cada funcion publica, solo para su rol.
-- ===========================================================================

do $roles$
declare
    v_table text;
    v_function regprocedure;
    v_role text;
begin
    foreach v_table in array array['agent_temporary_instructions', 'agent_temporary_instruction_events'] loop
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
        'public._agent_temporary_instruction_text_is_valid(text)',
        'public.reject_agent_temporary_instruction_mutation()',
        'public.validate_agent_temporary_instruction_change()',
        'public._agent_temporary_instruction_event(public.agent_temporary_instructions,text,text,text,text,jsonb)',
        'public._agent_temporary_instruction_expire_if_due(public.agent_temporary_instructions)',
        'public._agent_temporary_instruction_view(public.agent_temporary_instructions,boolean)',
        'public._agent_temporary_instruction_live(text,text)',
        'public.propose_agent_temporary_instruction_v1(jsonb,text,timestamptz,text,jsonb,text)',
        'public.get_agent_temporary_instruction_v1(jsonb,uuid)',
        'public.list_agent_temporary_instructions_v1(jsonb,integer)',
        'public.approver_get_agent_temporary_instruction_v1(jsonb,text,uuid)',
        'public.approver_publish_agent_temporary_instruction_v1(jsonb,text,uuid,text)',
        'public.approver_retire_agent_temporary_instruction_v1(jsonb,text,uuid,text)',
        'public.approver_discard_agent_temporary_instruction_v1(jsonb,text,uuid,text)',
        'public.get_live_agent_temporary_instruction_v1(jsonb)'
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
    -- Los roles los crean 20261021000100 y 20261028000100; una base sin ellos no los nombra.
    if to_regrole('automation_expert_change') is not null then
        foreach v_function in array array[
            'public.propose_agent_temporary_instruction_v1(jsonb,text,timestamptz,text,jsonb,text)',
            'public.get_agent_temporary_instruction_v1(jsonb,uuid)',
            'public.list_agent_temporary_instructions_v1(jsonb,integer)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to automation_expert_change', v_function);
        end loop;
    end if;
    if to_regrole('flow_change_approver') is not null then
        foreach v_function in array array[
            'public.approver_get_agent_temporary_instruction_v1(jsonb,text,uuid)',
            'public.approver_publish_agent_temporary_instruction_v1(jsonb,text,uuid,text)',
            'public.approver_retire_agent_temporary_instruction_v1(jsonb,text,uuid,text)',
            'public.approver_discard_agent_temporary_instruction_v1(jsonb,text,uuid,text)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end loop;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.get_live_agent_temporary_instruction_v1(jsonb) to service_role;
    end if;
end;
$roles$;

commit;
