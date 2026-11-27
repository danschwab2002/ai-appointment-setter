-- live-knowledge-v1: el conocimiento base editado por el Automation Expert y
-- servido en vivo (pieza 5.5, D4 del mapa de Z1 del 2026-11-26).
-- Contrato: docs/contracts/live-knowledge-v1.md.
--
-- El AE arma con el duenio un cambio por secciones sobre el conocimiento
-- vigente; el bridge lo valida con el mismo cargador del conocimiento y contra
-- la instancia, y la base lo guarda validado y congelado por 24 horas con su
-- huella. Una persona de la lista de aprobadores, desde la pagina, lo publica:
-- sin ese si nadie lo recibe. Publicado, el bridge lo sirve en vivo (lectura
-- con cache corta); volver al archivo del manifiesto es retirar la publicada.
--
-- 1. Dos tablas nuevas, vacias: agent_knowledge_versions (una fila por
--    version) y agent_knowledge_events (solo agregar). Sin permisos para
--    nadie de la API, con RLS.
-- 2. Tres funciones del Automation Expert, solo para automation_expert_change:
--    guardar una version validada, releerla y listarlas. Ni publicar, ni
--    descartar, ni volver al archivo.
-- 3. Cuatro funciones de la pagina, solo para flow_change_approver, con la
--    sesion viva (el actor sale de la fila de la sesion): releer, publicar,
--    descartar y volver al archivo.
-- 4. Una para service_role: la lectura de la publicada.
--
-- Una sola publicada por instancia: un indice unico parcial. Publicar retira
-- la anterior en la misma transaccion; la base exige que la propuesta se haya
-- armado sobre lo que esta publicado ahora (agent_knowledge_base_changed).
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- AGENT_LIVE_KNOWLEDGE_ENABLED el bridge no llama a ninguna de estas
-- funciones. Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 28000 sesion invalida; P0002 no existe o
-- no es de esta instancia. Los resultados del ciclo (tope, vencida, la base
-- cambio...) vuelven como {"ok": false, "reason_code"}.

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
        raise exception using errcode = '55000', message = 'agent_live_knowledge_prerequisite_missing';
    end if;
    if to_regclass('public.agent_knowledge_versions') is not null then
        raise exception using errcode = '55000', message = 'agent_live_knowledge_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. Las tablas.
-- ===========================================================================

create table public.agent_knowledge_versions (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    status text not null check (status in ('validada', 'publicada', 'retirada', 'vencida', 'descartada')),
    -- El numero de la version viva en la instancia (1, 2, 3...). No es el del archivo.
    live_version integer not null check (live_version > 0),
    -- El archivo del manifiesto sobre el que se armo: si cambia, la viva no se sirve.
    file_version integer not null check (file_version > 0),
    file_sha256 text not null check (file_sha256 ~ '^[0-9a-f]{64}$'),
    -- Lo vigente cuando el AE la armo: el archivo o una version viva publicada.
    base_origin text not null check (base_origin in ('archivo', 'vivo')),
    base_knowledge_id uuid references public.agent_knowledge_versions (id) on delete restrict,
    base_sha256 text not null check (base_sha256 ~ '^[0-9a-f]{64}$'),
    -- El cambio que mando el AE y el conocimiento entero que resulta (sin la cabecera).
    change jsonb not null check (jsonb_typeof(change) = 'object' and change ? 'cambios'
        and octet_length(change::text) <= 196608),
    knowledge jsonb not null check (jsonb_typeof(knowledge) = 'object' and not knowledge ? 'cabecera'
        and octet_length(knowledge::text) <= 262144),
    rendered_sha256 text not null check (rendered_sha256 ~ '^[0-9a-f]{64}$'),
    rendered_bytes integer not null check (rendered_bytes between 1 and 32000),
    preview jsonb not null check (jsonb_typeof(preview) = 'object' and octet_length(preview::text) <= 262144),
    validator jsonb not null check (jsonb_typeof(validator) = 'object' and octet_length(validator::text) <= 4096),
    purpose text not null check (public._flow_store_free_text_is_valid(purpose)),
    requested_by_kind text not null check (requested_by_kind = 'automation_expert'),
    requested_by_ref text not null,
    fingerprint text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
    validated_at timestamptz not null,
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
    constraint agent_knowledge_versions_base
        check ((base_origin = 'archivo') = (base_knowledge_id is null)),
    constraint agent_knowledge_versions_requested_by
        check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    constraint agent_knowledge_versions_approved_by
        check ((approved_by_kind is null and approved_by_ref is null)
               or public._flow_store_actor_is_valid(approved_by_kind, approved_by_ref)),
    constraint agent_knowledge_versions_closed_by
        check ((closed_by_kind is null and closed_by_ref is null)
               or public._flow_store_actor_is_valid(closed_by_kind, closed_by_ref)),
    constraint agent_knowledge_versions_window check (expires_at > validated_at),
    constraint agent_knowledge_versions_state_coherent check (coalesce(case status
        when 'validada' then approved_at is null and closed_at is null
        when 'publicada' then approved_at is not null and approved_by_kind is not null and closed_at is null
        when 'retirada' then approved_at is not null and closed_at is not null and closed_by_kind is not null
            and close_reason is not null
        when 'vencida' then approved_at is null and closed_at is not null and closed_by_kind = 'sistema'
        when 'descartada' then approved_at is null and closed_at is not null and closed_by_kind is not null
            and close_reason is not null
    end, false))
);

create table public.agent_knowledge_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    knowledge_id uuid not null references public.agent_knowledge_versions (id) on delete restrict,
    event_type text not null check (event_type in (
        'agent_knowledge_validated', 'agent_knowledge_published', 'agent_knowledge_publish_refused',
        'agent_knowledge_retired', 'agent_knowledge_discarded', 'agent_knowledge_expired')),
    actor_kind text not null check (actor_kind in ('automation_expert', 'slack', 'sistema')),
    actor_ref text not null,
    reason_code text not null check (reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'),
    data jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object'
        and octet_length(data::text) <= 16384),
    created_at timestamptz not null default clock_timestamp(),
    constraint agent_knowledge_events_actor
        check (public._flow_store_actor_is_valid(actor_kind, actor_ref))
);

-- **Una sola publicada por instancia.**
create unique index agent_knowledge_versions_one_live
    on public.agent_knowledge_versions (tenant_ref, ally_ref) where status = 'publicada';
create unique index agent_knowledge_versions_number_once
    on public.agent_knowledge_versions (tenant_ref, ally_ref, live_version);
create index agent_knowledge_versions_instance_time_idx
    on public.agent_knowledge_versions (tenant_ref, ally_ref, created_at desc);
create index agent_knowledge_events_version_time_idx
    on public.agent_knowledge_events (knowledge_id, created_at);

alter table public.agent_knowledge_versions enable row level security;
alter table public.agent_knowledge_events enable row level security;

-- ===========================================================================
-- 2. Los disparadores.
-- ===========================================================================

create function public.reject_agent_knowledge_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'agent_knowledge_events' then 'agent_knowledge_event_is_append_only'
        else 'agent_knowledge_is_permanent'
    end;
end;
$function$;

-- El contenido nace congelado: solo cambian el estado y su rastro. Un estado
-- final no acepta ningun UPDATE.
create function public.validate_agent_knowledge_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if old.status in ('retirada', 'vencida', 'descartada') then
        raise exception using errcode = '55000', message = 'agent_knowledge_frozen';
    end if;
    if row(new.id, new.tenant_ref, new.ally_ref, new.live_version, new.file_version, new.file_sha256,
           new.base_origin, new.base_knowledge_id, new.base_sha256, new.change, new.knowledge,
           new.rendered_sha256, new.rendered_bytes, new.preview, new.validator, new.purpose,
           new.requested_by_kind, new.requested_by_ref, new.fingerprint, new.validated_at, new.expires_at,
           new.created_at)
       is distinct from row(old.id, old.tenant_ref, old.ally_ref, old.live_version, old.file_version,
           old.file_sha256, old.base_origin, old.base_knowledge_id, old.base_sha256, old.change, old.knowledge,
           old.rendered_sha256, old.rendered_bytes, old.preview, old.validator, old.purpose,
           old.requested_by_kind, old.requested_by_ref, old.fingerprint, old.validated_at, old.expires_at,
           old.created_at) then
        raise exception using errcode = '55000', message = 'agent_knowledge_frozen';
    end if;
    if new.status is distinct from old.status and not (
        (old.status = 'validada' and new.status in ('publicada', 'descartada', 'vencida'))
        or (old.status = 'publicada' and new.status = 'retirada')
    ) then
        raise exception using errcode = '23514', message = 'agent_knowledge_transition_invalid';
    end if;
    if old.status = 'publicada'
       and row(new.approved_at, new.approved_by_kind, new.approved_by_ref)
           is distinct from row(old.approved_at, old.approved_by_kind, old.approved_by_ref) then
        raise exception using errcode = '55000', message = 'agent_knowledge_frozen';
    end if;
    return new;
end;
$function$;

create trigger agent_knowledge_versions_validate
before update on public.agent_knowledge_versions
for each row execute function public.validate_agent_knowledge_change();
create trigger agent_knowledge_versions_permanent
before delete on public.agent_knowledge_versions
for each row execute function public.reject_agent_knowledge_mutation();
create trigger agent_knowledge_versions_no_truncate
before truncate on public.agent_knowledge_versions
for each statement execute function public.reject_agent_knowledge_mutation();
create trigger agent_knowledge_versions_set_updated_at
before update on public.agent_knowledge_versions
for each row execute function public.set_updated_at();
create trigger agent_knowledge_events_append_only
before update or delete on public.agent_knowledge_events
for each row execute function public.reject_agent_knowledge_mutation();
create trigger agent_knowledge_events_no_truncate
before truncate on public.agent_knowledge_events
for each statement execute function public.reject_agent_knowledge_mutation();

-- ===========================================================================
-- 3. Los helpers internos (security invoker, de nadie de la API).
-- ===========================================================================

create function public._agent_knowledge_event(
    p_version public.agent_knowledge_versions, p_event_type text, p_actor_kind text, p_actor_ref text,
    p_reason_code text, p_data jsonb)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.agent_knowledge_events (tenant_ref, ally_ref, knowledge_id, event_type,
        actor_kind, actor_ref, reason_code, data)
    values (p_version.tenant_ref, p_version.ally_ref, p_version.id, p_event_type,
        p_actor_kind, p_actor_ref, p_reason_code, coalesce(p_data, '{}'::jsonb))
$function$;

-- La huella la calcula solo la base, sobre el texto del jsonb, al guardar.
create function public._agent_knowledge_fingerprint(
    p_id uuid, p_live_version integer, p_file_sha256 text, p_base_sha256 text, p_change jsonb,
    p_knowledge jsonb, p_rendered_sha256 text, p_purpose text)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select encode(sha256(convert_to(jsonb_build_object(
        'contract', 'live-knowledge-v1', 'knowledge_id', p_id, 'live_version', p_live_version,
        'file_sha256', p_file_sha256, 'base_sha256', p_base_sha256, 'change', p_change,
        'knowledge', p_knowledge, 'rendered_sha256', p_rendered_sha256, 'purpose', p_purpose)::text, 'UTF8')), 'hex')
$function$;

-- Con la fila ya bloqueada: si esta validada y vencio, la pasa a vencida.
create function public._agent_knowledge_expire_if_due(p_version public.agent_knowledge_versions)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_version.status is distinct from 'validada'
       or not coalesce(clock_timestamp() >= p_version.expires_at, false) then
        return false;
    end if;
    update public.agent_knowledge_versions
       set status = 'vencida', closed_at = clock_timestamp(), closed_by_kind = 'sistema', closed_by_ref = 'vencimiento'
     where id = p_version.id;
    perform public._agent_knowledge_event(p_version, 'agent_knowledge_expired', 'sistema', 'vencimiento',
        'agent_knowledge_expired', jsonb_build_object('expires_at', p_version.expires_at));
    return true;
end;
$function$;

-- Lo que se muestra de una version. Con p_with_people (la pagina), el ID de
-- Slack de quien la aprobo; el agente no lo recibe. Nunca quien la pidio.
create function public._agent_knowledge_view(p_version public.agent_knowledge_versions, p_with_people boolean)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'knowledge_id', p_version.id,
        'status', p_version.status,
        'effective_status', case
            when p_version.status = 'validada' and clock_timestamp() >= p_version.expires_at then 'vencida'
            else p_version.status end,
        'live_version', p_version.live_version,
        'file_version', p_version.file_version,
        'file_sha256', p_version.file_sha256,
        'base_origin', p_version.base_origin,
        'base_knowledge_id', p_version.base_knowledge_id,
        'base_sha256', p_version.base_sha256,
        'change', p_version.change,
        'knowledge', p_version.knowledge,
        'rendered_sha256', p_version.rendered_sha256,
        'rendered_bytes', p_version.rendered_bytes,
        'preview', p_version.preview,
        'purpose', p_version.purpose,
        'fingerprint', p_version.fingerprint,
        'validated_at', p_version.validated_at,
        'expires_at', p_version.expires_at,
        'approved_at', p_version.approved_at,
        'closed_at', p_version.closed_at,
        'closed_by_kind', p_version.closed_by_kind,
        'created_at', p_version.created_at,
        'live', (select jsonb_build_object('knowledge_id', l.id, 'live_version', l.live_version,
                                           'rendered_sha256', l.rendered_sha256)
                   from public.agent_knowledge_versions l
                  where l.tenant_ref = p_version.tenant_ref and l.ally_ref = p_version.ally_ref
                    and l.status = 'publicada'),
        'events', coalesce((
            select jsonb_agg(jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                'reason_code', e.reason_code, 'created_at', e.created_at)
                             order by e.created_at desc, e.id)
              from (select * from public.agent_knowledge_events e
                     where e.knowledge_id = p_version.id
                     order by e.created_at desc, e.id limit 50) e), '[]'::jsonb))
        || case when p_with_people then jsonb_build_object('approved_by_ref', p_version.approved_by_ref,
                                                           'closed_by_ref', p_version.closed_by_ref)
                else '{}'::jsonb end
$function$;

-- ===========================================================================
-- 4. Las tres del Automation Expert.
-- ===========================================================================

-- Guardar una version validada. La base no corre las reglas (lo hace el
-- bridge con el cargador): mira la forma, el tope de abiertas y que la base
-- de la propuesta sea lo publicado ahora.
create function public.save_agent_knowledge_version_v1(
    p_instance jsonb, p_file_version integer, p_file_sha256 text, p_base_origin text, p_base_knowledge_id uuid,
    p_base_sha256 text, p_change jsonb, p_knowledge jsonb, p_rendered_sha256 text, p_rendered_bytes integer,
    p_preview jsonb, p_validator jsonb, p_purpose text, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_version public.agent_knowledge_versions%rowtype;
    v_live public.agent_knowledge_versions%rowtype;
    v_open integer;
    v_number integer;
    v_id uuid;
    v_now timestamptz;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_purpose) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    if p_base_origin is null or p_base_origin not in ('archivo', 'vivo')
       or (p_base_origin = 'archivo') is distinct from (p_base_knowledge_id is null) then
        raise exception using errcode = '22023', message = 'agent_knowledge_base_invalid';
    end if;
    perform set_config('lock_timeout', '5s', true);
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_knowledge:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_live from public.agent_knowledge_versions l
     where l.tenant_ref = v_instance.o_tenant_ref and l.ally_ref = v_instance.o_ally_ref and l.status = 'publicada';
    if (p_base_origin = 'archivo' and found)
       or (p_base_origin = 'vivo' and (not found or v_live.id is distinct from p_base_knowledge_id
                                       or v_live.rendered_sha256 is distinct from p_base_sha256)) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_base_changed');
    end if;
    select count(*)::integer into v_open from public.agent_knowledge_versions o
     where o.tenant_ref = v_instance.o_tenant_ref and o.ally_ref = v_instance.o_ally_ref
       and o.status = 'validada' and clock_timestamp() < o.expires_at;
    if v_open >= 3 then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_open_limit', 'open', v_open);
    end if;
    select coalesce(max(n.live_version), 0) + 1 into v_number from public.agent_knowledge_versions n
     where n.tenant_ref = v_instance.o_tenant_ref and n.ally_ref = v_instance.o_ally_ref;
    v_id := gen_random_uuid();
    v_now := clock_timestamp();
    insert into public.agent_knowledge_versions (id, tenant_ref, ally_ref, status, live_version, file_version,
        file_sha256, base_origin, base_knowledge_id, base_sha256, change, knowledge, rendered_sha256,
        rendered_bytes, preview, validator, purpose, requested_by_kind, requested_by_ref, fingerprint,
        validated_at, expires_at)
    values (v_id, v_instance.o_tenant_ref, v_instance.o_ally_ref, 'validada', v_number, p_file_version,
        p_file_sha256, p_base_origin, p_base_knowledge_id, p_base_sha256, p_change, p_knowledge,
        p_rendered_sha256, p_rendered_bytes, p_preview, p_validator, p_purpose, 'automation_expert',
        p_requested_by_ref, public._agent_knowledge_fingerprint(v_id, v_number, p_file_sha256, p_base_sha256,
            p_change, p_knowledge, p_rendered_sha256, p_purpose),
        v_now, v_now + interval '24 hours')
    returning * into v_version;
    perform public._agent_knowledge_event(v_version, 'agent_knowledge_validated', 'automation_expert',
        p_requested_by_ref, 'agent_knowledge_validated', jsonb_build_object(
            'live_version', v_number, 'rendered_sha256', p_rendered_sha256,
            'change_sha256', encode(sha256(convert_to(p_change::text, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'knowledge_id', v_version.id, 'live_version', v_version.live_version,
        'status', v_version.status, 'fingerprint', v_version.fingerprint, 'expires_at', v_version.expires_at);
end;
$function$;

create function public.get_agent_knowledge_version_v1(p_instance jsonb, p_knowledge_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_version public.agent_knowledge_versions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_version from public.agent_knowledge_versions v
     where v.id = p_knowledge_id and v.tenant_ref = v_instance.o_tenant_ref and v.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_knowledge_not_found';
    end if;
    return public._agent_knowledge_view(v_version, false);
end;
$function$;

create function public.list_agent_knowledge_versions_v1(p_instance jsonb, p_only_open boolean, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_rows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_only_open is null or p_limit is null or p_limit not between 1 and 50 then
        raise exception using errcode = '22023', message = 'agent_knowledge_list_invalid';
    end if;
    select coalesce(jsonb_agg(jsonb_build_object(
               'knowledge_id', v.id, 'status', v.status,
               'effective_status', case when v.status = 'validada' and clock_timestamp() >= v.expires_at
                                        then 'vencida' else v.status end,
               'live_version', v.live_version, 'file_version', v.file_version, 'purpose', v.purpose,
               'sections', (select coalesce(jsonb_agg(k order by k), '[]'::jsonb)
                              from jsonb_object_keys(v.change -> 'cambios') k),
               'expires_at', v.expires_at, 'approved_at', v.approved_at, 'closed_at', v.closed_at,
               'created_at', v.created_at) order by v.created_at desc, v.id), '[]'::jsonb)
      into v_rows
      from (select * from public.agent_knowledge_versions v
             where v.tenant_ref = v_instance.o_tenant_ref and v.ally_ref = v_instance.o_ally_ref
               and (not p_only_open or v.status = 'publicada'
                    or (v.status = 'validada' and clock_timestamp() < v.expires_at))
             order by v.created_at desc, v.id limit p_limit) v;
    return jsonb_build_object('versions', v_rows, 'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 5. Las cuatro de la pagina (flow_change_approver, con la sesion viva).
-- ===========================================================================

create function public.approver_get_agent_knowledge_v1(p_instance jsonb, p_session_hash text, p_knowledge_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_version public.agent_knowledge_versions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_version from public.agent_knowledge_versions v
     where v.id = p_knowledge_id and v.tenant_ref = v_instance.o_tenant_ref and v.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_knowledge_not_found';
    end if;
    return public._agent_knowledge_view(v_version, true);
end;
$function$;

-- Publicar: el si. La huella y el hash del render viajan con el si; el bridge
-- ya rehizo la version y corrio las reglas. La base exige que la propuesta se
-- haya armado sobre lo publicado ahora, y retira la anterior.
create function public.approver_publish_agent_knowledge_v1(
    p_instance jsonb, p_session_hash text, p_knowledge_id uuid, p_fingerprint text, p_rendered_sha256 text)
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
    v_version public.agent_knowledge_versions%rowtype;
    v_live public.agent_knowledge_versions%rowtype;
    v_now timestamptz;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_knowledge_fingerprint_invalid';
    end if;
    if not coalesce(p_rendered_sha256 ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_knowledge_rendered_sha_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_knowledge:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_version from public.agent_knowledge_versions v
     where v.id = p_knowledge_id and v.tenant_ref = v_instance.o_tenant_ref and v.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_knowledge_not_found';
    end if;
    if public._agent_knowledge_expire_if_due(v_version) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_expired');
    end if;
    if p_fingerprint is distinct from v_version.fingerprint
       or p_rendered_sha256 is distinct from v_version.rendered_sha256 then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_fingerprint_mismatch');
    end if;
    if v_version.status = 'publicada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', v_version.status);
    end if;
    if v_version.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_not_validated',
            'status', v_version.status);
    end if;
    select * into v_live from public.agent_knowledge_versions l
     where l.tenant_ref = v_instance.o_tenant_ref and l.ally_ref = v_instance.o_ally_ref and l.status = 'publicada'
     for update;
    if (v_version.base_origin = 'archivo' and found)
       or (v_version.base_origin = 'vivo' and (not found or v_live.id is distinct from v_version.base_knowledge_id)) then
        perform public._agent_knowledge_event(v_version, 'agent_knowledge_publish_refused', 'slack', v_actor,
            'agent_knowledge_base_changed', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_base_changed');
    end if;
    v_now := clock_timestamp();
    if v_live.id is not null then
        update public.agent_knowledge_versions
           set status = 'retirada', closed_at = v_now, closed_by_kind = 'slack', closed_by_ref = v_actor,
               close_reason = 'reemplazada por la version ' || v_version.live_version
         where id = v_live.id;
        perform public._agent_knowledge_event(v_live, 'agent_knowledge_retired', 'slack', v_actor,
            'agent_knowledge_replaced', jsonb_build_object('by_live_version', v_version.live_version));
    end if;
    update public.agent_knowledge_versions
       set status = 'publicada', approved_at = v_now, approved_by_kind = 'slack', approved_by_ref = v_actor
     where id = v_version.id;
    perform public._agent_knowledge_event(v_version, 'agent_knowledge_published', 'slack', v_actor,
        'agent_knowledge_published', jsonb_build_object('fingerprint', v_version.fingerprint,
            'rendered_sha256', v_version.rendered_sha256, 'live_version', v_version.live_version));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada');
end;
$function$;

create function public.approver_discard_agent_knowledge_v1(
    p_instance jsonb, p_session_hash text, p_knowledge_id uuid, p_reason text)
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
    v_version public.agent_knowledge_versions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_version from public.agent_knowledge_versions v
     where v.id = p_knowledge_id and v.tenant_ref = v_instance.o_tenant_ref and v.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_knowledge_not_found';
    end if;
    if public._agent_knowledge_expire_if_due(v_version) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_final', 'status', 'vencida');
    end if;
    if v_version.status = 'descartada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_version.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_final', 'status', v_version.status);
    end if;
    update public.agent_knowledge_versions
       set status = 'descartada', closed_at = clock_timestamp(), closed_by_kind = 'slack', closed_by_ref = v_actor,
           close_reason = p_reason
     where id = v_version.id;
    perform public._agent_knowledge_event(v_version, 'agent_knowledge_discarded', 'slack', v_actor,
        'agent_knowledge_discarded', jsonb_build_object(
            'close_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

-- Volver al archivo: retira la publicada. En la proxima lectura el bridge
-- sirve el archivo del manifiesto. Sin publicada no hay nada que retirar.
create function public.approver_return_to_file_agent_knowledge_v1(
    p_instance jsonb, p_session_hash text, p_reason text)
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
    v_live public.agent_knowledge_versions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_knowledge:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_live from public.agent_knowledge_versions l
     where l.tenant_ref = v_instance.o_tenant_ref and l.ally_ref = v_instance.o_ally_ref and l.status = 'publicada'
     for update;
    if not found then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_knowledge_no_live');
    end if;
    update public.agent_knowledge_versions
       set status = 'retirada', closed_at = clock_timestamp(), closed_by_kind = 'slack', closed_by_ref = v_actor,
           close_reason = p_reason
     where id = v_live.id;
    perform public._agent_knowledge_event(v_live, 'agent_knowledge_retired', 'slack', v_actor,
        'agent_knowledge_returned_to_file', jsonb_build_object(
            'close_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'retired_live_version', v_live.live_version);
end;
$function$;

-- ===========================================================================
-- 6. La de service_role: lo que sirve el bridge.
-- ===========================================================================

-- La publicada de la instancia (a lo sumo una), sin IDs de Slack salvo el de
-- quien aprobo (no llega al agente: el render no lo usa). El bridge rehace la
-- version y decide si se sirve.
create function public.get_live_agent_knowledge_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_row jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    select jsonb_build_object(
               'knowledge_id', v.id, 'live_version', v.live_version, 'file_version', v.file_version,
               'file_sha256', v.file_sha256, 'knowledge', v.knowledge, 'rendered_sha256', v.rendered_sha256,
               'approved_at', v.approved_at, 'approved_by_ref', v.approved_by_ref)
      into v_row
      from public.agent_knowledge_versions v
     where v.tenant_ref = v_instance.o_tenant_ref and v.ally_ref = v_instance.o_ally_ref
       and v.status = 'publicada';
    return jsonb_build_object('knowledge', v_row, 'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 7. Los permisos. Las tablas, para nadie de la API. Las internas y los
--    disparadores, para nadie. Cada funcion publica, solo para su rol.
-- ===========================================================================

do $roles$
declare
    v_table text;
    v_function regprocedure;
    v_role text;
begin
    foreach v_table in array array['agent_knowledge_versions', 'agent_knowledge_events'] loop
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
        'public.reject_agent_knowledge_mutation()',
        'public.validate_agent_knowledge_change()',
        'public._agent_knowledge_event(public.agent_knowledge_versions,text,text,text,text,jsonb)',
        'public._agent_knowledge_fingerprint(uuid,integer,text,text,jsonb,jsonb,text,text)',
        'public._agent_knowledge_expire_if_due(public.agent_knowledge_versions)',
        'public._agent_knowledge_view(public.agent_knowledge_versions,boolean)',
        'public.save_agent_knowledge_version_v1(jsonb,integer,text,text,uuid,text,jsonb,jsonb,text,integer,jsonb,jsonb,text,text)',
        'public.get_agent_knowledge_version_v1(jsonb,uuid)',
        'public.list_agent_knowledge_versions_v1(jsonb,boolean,integer)',
        'public.approver_get_agent_knowledge_v1(jsonb,text,uuid)',
        'public.approver_publish_agent_knowledge_v1(jsonb,text,uuid,text,text)',
        'public.approver_discard_agent_knowledge_v1(jsonb,text,uuid,text)',
        'public.approver_return_to_file_agent_knowledge_v1(jsonb,text,text)',
        'public.get_live_agent_knowledge_v1(jsonb)'
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
    if to_regrole('automation_expert_change') is not null then
        foreach v_function in array array[
            'public.save_agent_knowledge_version_v1(jsonb,integer,text,text,uuid,text,jsonb,jsonb,text,integer,jsonb,jsonb,text,text)',
            'public.get_agent_knowledge_version_v1(jsonb,uuid)',
            'public.list_agent_knowledge_versions_v1(jsonb,boolean,integer)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to automation_expert_change', v_function);
        end loop;
    end if;
    if to_regrole('flow_change_approver') is not null then
        foreach v_function in array array[
            'public.approver_get_agent_knowledge_v1(jsonb,text,uuid)',
            'public.approver_publish_agent_knowledge_v1(jsonb,text,uuid,text,text)',
            'public.approver_discard_agent_knowledge_v1(jsonb,text,uuid,text)',
            'public.approver_return_to_file_agent_knowledge_v1(jsonb,text,text)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end loop;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.get_live_agent_knowledge_v1(jsonb) to service_role;
    end if;
end;
$roles$;

commit;
