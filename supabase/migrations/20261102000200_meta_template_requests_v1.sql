-- meta-template-requests-v1: la solicitud de plantilla de Meta, hermana de la
-- propuesta de flujo (F-146, partes 1 y 2 del diseno E-258).
-- Contrato: docs/contracts/template-request-v1.md. Diseno:
-- productos/soporte-infoproductores/documentacion/2026-11-01-f146-diseno-plantillas-a-meta-desde-el-ae.md
-- (vault del OS), §3 a §6.
--
-- Cuando un flujo necesita una plantilla que no existe, el Automation Expert
-- la redacta con el duenio y la deja como solicitud: un borrador con
-- revisiones, una validacion (que corre el bridge) que lo congela con su
-- huella por 24 horas, la aprobacion de una persona de la lista de la
-- instancia (la sesion de la pagina de aprobacion) y despues el envio a Meta y
-- lo que Meta contesta. Esta migracion es solo la base: nada llama a Meta, y
-- sin META_TEMPLATES_ENABLED el bridge no llama a ninguna de estas funciones.
--
-- 1. Dos tablas nuevas, vacias: meta_template_requests (una fila por
--    solicitud, con el recorrido de estados en un CHECK y en un disparador) y
--    meta_template_request_events (solo agregar). Sin permisos para nadie de la
--    API, con RLS.
-- 2. Cuatro funciones del Automation Expert, solo para automation_expert_change
--    (20261021000100): guardar o rehacer un borrador (con sus topes: 10
--    abiertas, 100 revisiones y 100 validaciones), registrar la validacion,
--    releer una solicitud y listarlas. Ni aprobar, ni mandar, ni descartar.
-- 3. Cinco funciones de la pagina, solo para flow_change_approver
--    (20261028000100), todas con la sesion viva: el actor slack sale de la
--    fila de la sesion, nunca de un parametro. Releer, aprobar (un solo paso,
--    D3), descartar, empezar el envio (con el tope por dia, D8) y registrar lo
--    que contesto Meta al crearla. La lista de aprobadores la mira el bridge,
--    como con los flujos.
-- 4. Una para service_role: registrar el estado que leyo el sondeo (D2), con
--    la aprobacion, el rechazo, la falta de respuesta a las 48 horas y la
--    entrada al catalogo de la bandeja.
--
-- La categoria final se compara con la pedida: una de utilidad que Meta
-- aprueba como marketing queda aprobada_meta con recategorized = true (desde
-- el 09/04/2025 Meta recategoriza en vez de rechazar; paso con
-- maribel_retomar_proceso_01 y att1_ficha_vence_manana_01).
--
-- Un nombre con su idioma se usa una sola vez por instancia: Meta no deja
-- repetirlo, ni siquiera borrado. Solo una solicitud descartada o vencida (que
-- nunca llego a Meta) lo libera. Corregir un rechazo es otra solicitud con el
-- numero siguiente (D5).
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y no cambia
-- lo que se manda. Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 28000 sesion invalida
-- (flow_approval_session_invalid); P0002 no existe o no es de esta instancia;
-- 55000 un estado que no lo permite. Los resultados del ciclo (revision vieja,
-- vencida, tope, nombre ocupado...) vuelven como {"ok": false, "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_store_actor_is_valid(text,text)') is null
       or to_regprocedure('public._flow_store_free_text_is_valid(text)') is null
       or to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regprocedure('public.set_updated_at()') is null
       or to_regclass('public.flow_change_proposals') is null then
        raise exception using errcode = '55000', message = 'meta_template_requests_prerequisite_missing';
    end if;
    if to_regclass('public.meta_template_requests') is not null then
        raise exception using errcode = '55000', message = 'meta_template_requests_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. Las formas que usan los CHECK.
-- ===========================================================================

-- El borrador normalizado que guarda el bridge (template_request.py). La base
-- no corre las reglas de Meta: mira la forma, para que una fila no pueda
-- traer otra cosa.
create function public._meta_template_draft_is_valid(p_draft jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
declare
    v_keys text[];
begin
    if jsonb_typeof(p_draft) is distinct from 'object' or octet_length(p_draft::text) > 16384 then
        return false;
    end if;
    select array_agg(k order by k) into v_keys from jsonb_object_keys(p_draft) k;
    if not v_keys <@ array['botones', 'categoria', 'cuerpo', 'encabezado', 'idioma', 'nombre', 'pie', 'variables']
       or not v_keys @> array['botones', 'categoria', 'cuerpo', 'idioma', 'nombre', 'variables'] then
        return false;
    end if;
    return coalesce(
        jsonb_typeof(p_draft -> 'nombre') = 'string'
        and p_draft ->> 'nombre' ~ '^[a-z0-9][a-z0-9_]{1,99}$'
        and jsonb_typeof(p_draft -> 'idioma') = 'string'
        and p_draft ->> 'idioma' ~ '^[a-z]{2,3}(_[A-Z]{2})?$'
        and p_draft -> 'categoria' in ('"UTILITY"'::jsonb, '"MARKETING"'::jsonb)
        and jsonb_typeof(p_draft -> 'cuerpo') = 'string'
        and length(p_draft ->> 'cuerpo') between 1 and 1024
        and jsonb_typeof(p_draft -> 'variables') = 'array'
        and jsonb_array_length(p_draft -> 'variables') <= 10
        and jsonb_typeof(p_draft -> 'botones') = 'array'
        and jsonb_array_length(p_draft -> 'botones') <= 10
        and (not p_draft ? 'encabezado' or (jsonb_typeof(p_draft -> 'encabezado') = 'string'
             and length(p_draft ->> 'encabezado') between 1 and 60))
        and (not p_draft ? 'pie' or (jsonb_typeof(p_draft -> 'pie') = 'string'
             and length(p_draft ->> 'pie') between 1 and 60)), false);
end;
$function$;

-- El pedido de Meta que congela la validacion: el nombre, el idioma y la
-- categoria son los de la fila, y va posicional (el bridge numera).
create function public._meta_template_meta_request_is_valid(
    p_meta_request jsonb, p_name text, p_language text, p_category text)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    if jsonb_typeof(p_meta_request) is distinct from 'object' or octet_length(p_meta_request::text) > 16384 then
        return false;
    end if;
    if (select array_agg(k order by k) from jsonb_object_keys(p_meta_request) k)
       is distinct from array['category', 'components', 'language', 'name', 'parameter_format'] then
        return false;
    end if;
    return coalesce(
        p_meta_request ->> 'name' = p_name
        and p_meta_request ->> 'language' = p_language
        and p_meta_request ->> 'category' = p_category
        and p_meta_request ->> 'parameter_format' = 'POSITIONAL'
        and jsonb_typeof(p_meta_request -> 'components') = 'array'
        and jsonb_array_length(p_meta_request -> 'components') between 1 and 4, false);
end;
$function$;

-- ===========================================================================
-- 2. Las tablas.
-- ===========================================================================

create table public.meta_template_requests (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    status text not null check (status in (
        'borrador', 'validada', 'aprobada', 'enviada', 'en_revision', 'sin_respuesta_de_meta',
        'aprobada_meta', 'rechazada', 'en_catalogo', 'vencida', 'descartada')),
    revision integer not null check (revision > 0),
    template_name text not null check (template_name ~ '^[a-z0-9][a-z0-9_]{1,99}$'),
    language text not null check (language ~ '^[a-z]{2,3}(_[A-Z]{2})?$'),
    category_requested text not null check (category_requested in ('UTILITY', 'MARKETING')),
    draft jsonb not null check (public._meta_template_draft_is_valid(draft)),
    purpose text not null check (public._flow_store_free_text_is_valid(purpose)),
    -- Para que flujo es: la propuesta y el paso (JSON Pointer), si viene de una.
    proposal_id uuid references public.flow_change_proposals (id) on delete restrict,
    step_pointer text check (step_pointer ~ '^(/[a-z0-9_]{1,40}){1,16}$'),
    requested_by_kind text not null check (requested_by_kind = 'automation_expert'),
    requested_by_ref text not null,
    validation jsonb check (validation is null or (jsonb_typeof(validation) = 'object'
        and octet_length(validation::text) <= 131072)),
    fingerprint text check (fingerprint ~ '^[0-9a-f]{64}$'),
    validated_at timestamptz,
    expires_at timestamptz,
    approved_at timestamptz,
    approved_by_kind text check (approved_by_kind in ('operador', 'slack')),
    approved_by_ref text,
    -- El envio: sent_at es el primer intento (cuenta para el tope por dia),
    -- send_started_at el ultimo.
    sent_at timestamptz,
    send_started_at timestamptz,
    send_attempts integer not null default 0 check (send_attempts between 0 and 5),
    last_send_error text check (last_send_error ~ '^[a-z][a-z0-9_:]{0,79}$'),
    -- Lo que contesto Meta.
    meta_template_id text check (meta_template_id ~ '^[0-9]{1,32}$'),
    meta_status text check (meta_status ~ '^[A-Z][A-Z_]{1,31}$'),
    meta_category text check (meta_category in ('UTILITY', 'MARKETING', 'AUTHENTICATION')),
    meta_rejected_reason text check (meta_rejected_reason ~ '^[A-Z][A-Z_]{1,63}$'),
    meta_answered_at timestamptz,
    last_polled_at timestamptz,
    in_catalog_at timestamptz,
    closed_at timestamptz,
    closed_by_kind text check (closed_by_kind in ('operador', 'slack', 'sistema')),
    closed_by_ref text,
    close_reason text check (close_reason is null or public._flow_store_free_text_is_valid(close_reason)),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    constraint meta_template_requests_requested_by
        check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    constraint meta_template_requests_approved_by
        check ((approved_by_kind is null and approved_by_ref is null)
               or public._flow_store_actor_is_valid(approved_by_kind, approved_by_ref)),
    constraint meta_template_requests_closed_by
        check ((closed_by_kind is null and closed_by_ref is null)
               or public._flow_store_actor_is_valid(closed_by_kind, closed_by_ref)),
    constraint meta_template_requests_identity_matches_draft
        check (draft ->> 'nombre' = template_name and draft ->> 'idioma' = language
               and draft ->> 'categoria' = category_requested),
    constraint meta_template_requests_flow_link
        check ((proposal_id is null) = (step_pointer is null)),
    -- El CASE entero adentro de coalesce: una comparacion con NULL no deja
    -- pasar la fila.
    constraint meta_template_requests_state_coherent check (coalesce(case status
        when 'borrador' then fingerprint is null and validated_at is null and expires_at is null
            and approved_at is null and approved_by_kind is null and sent_at is null
            and send_started_at is null and send_attempts = 0 and meta_template_id is null
            and meta_status is null and closed_at is null and closed_by_kind is null
        when 'validada' then fingerprint is not null and validation is not null and validated_at is not null
            and expires_at = validated_at + interval '24 hours' and approved_at is null
            and approved_by_kind is null and sent_at is null and meta_template_id is null
            and closed_at is null and closed_by_kind is null
        when 'aprobada' then fingerprint is not null and validation is not null
            and expires_at = validated_at + interval '24 hours' and approved_at is not null
            and approved_by_kind is not null and sent_at is null and meta_template_id is null
            and closed_at is null and closed_by_kind is null
        when 'enviada' then fingerprint is not null and approved_at is not null and approved_by_kind is not null
            and sent_at is not null and send_started_at is not null and send_attempts >= 1
            and meta_template_id is null and closed_at is null
        when 'en_revision' then approved_at is not null and sent_at is not null
            and meta_template_id is not null and meta_status is not null and closed_at is null
        when 'sin_respuesta_de_meta' then approved_at is not null and sent_at is not null
            and meta_template_id is not null and meta_status is not null and closed_at is null
        when 'aprobada_meta' then approved_at is not null and meta_template_id is not null
            and meta_category is not null and meta_answered_at is not null and closed_at is null
        when 'rechazada' then approved_at is not null and meta_template_id is not null
            and meta_answered_at is not null and closed_at is not null and closed_by_kind = 'sistema'
        when 'en_catalogo' then approved_at is not null and meta_template_id is not null
            and meta_category is not null and meta_answered_at is not null and in_catalog_at is not null
            and closed_at is not null and closed_by_kind = 'sistema'
        when 'vencida' then fingerprint is not null and expires_at is not null and sent_at is null
            and closed_at is not null and closed_by_kind = 'sistema'
        when 'descartada' then sent_at is null and closed_at is not null and closed_by_kind is not null
            and close_reason is not null
    end, false))
);

-- El id es un uuid y no un identity: una secuencia recibiria permisos por los
-- privilegios por defecto de Supabase.
create table public.meta_template_request_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    request_id uuid not null references public.meta_template_requests (id) on delete restrict,
    event_type text not null check (event_type in (
        'meta_template_draft_saved', 'meta_template_validation_blocked', 'meta_template_validated',
        'meta_template_approved', 'meta_template_discarded', 'meta_template_expired',
        'meta_template_send_started', 'meta_template_send_refused', 'meta_template_sent',
        'meta_template_send_failed', 'meta_template_in_review', 'meta_template_no_answer',
        'meta_template_approved_by_meta', 'meta_template_rejected_by_meta', 'meta_template_in_catalog',
        'meta_template_status_other')),
    actor_kind text not null check (actor_kind in ('automation_expert', 'operador', 'slack', 'sistema')),
    actor_ref text not null,
    reason_code text not null check (reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'),
    data jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object'
        and octet_length(data::text) <= 16384),
    created_at timestamptz not null default clock_timestamp(),
    constraint meta_template_request_events_actor
        check (public._flow_store_actor_is_valid(actor_kind, actor_ref))
);

-- Un nombre con su idioma, una sola vez por instancia. Libera el nombre solo
-- lo que nunca llego a Meta.
create unique index meta_template_requests_name_once
    on public.meta_template_requests (tenant_ref, ally_ref, template_name, language)
    where status not in ('descartada', 'vencida');
-- Un id de Meta es de una sola solicitud.
create unique index meta_template_requests_meta_id_once
    on public.meta_template_requests (tenant_ref, ally_ref, meta_template_id)
    where meta_template_id is not null;
create index meta_template_requests_instance_time_idx
    on public.meta_template_requests (tenant_ref, ally_ref, created_at desc);
create index meta_template_requests_open_idx on public.meta_template_requests (tenant_ref, ally_ref)
    where status in ('borrador', 'validada', 'aprobada');
create index meta_template_requests_sent_idx on public.meta_template_requests (tenant_ref, ally_ref, sent_at)
    where sent_at is not null;
create index meta_template_requests_proposal_idx on public.meta_template_requests (proposal_id)
    where proposal_id is not null;
create index meta_template_request_events_request_time_idx
    on public.meta_template_request_events (request_id, created_at);

alter table public.meta_template_requests enable row level security;
alter table public.meta_template_request_events enable row level security;

-- ===========================================================================
-- 3. Los disparadores.
-- ===========================================================================

create function public.reject_meta_template_request_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'meta_template_request_events' then 'meta_template_request_event_is_append_only'
        else 'meta_template_request_is_permanent'
    end;
end;
$function$;

-- El recorrido de la solicitud. Desde validada, el contenido, la huella, las
-- fechas y la validacion quedan congelados; desde aprobada, tambien la
-- aprobacion; un estado final no acepta ningun UPDATE.
create function public.validate_meta_template_request_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if old.status in ('rechazada', 'en_catalogo', 'vencida', 'descartada') then
        raise exception using errcode = '55000', message = 'meta_template_request_frozen';
    end if;
    if row(new.id, new.tenant_ref, new.ally_ref, new.requested_by_kind, new.requested_by_ref, new.created_at)
       is distinct from row(old.id, old.tenant_ref, old.ally_ref, old.requested_by_kind, old.requested_by_ref,
                            old.created_at) then
        raise exception using errcode = '55000', message = 'meta_template_request_identity_is_immutable';
    end if;
    if new.status is distinct from old.status and not (
        (old.status = 'borrador' and new.status in ('validada', 'descartada'))
        or (old.status = 'validada' and new.status in ('aprobada', 'descartada', 'vencida'))
        or (old.status = 'aprobada' and new.status in ('enviada', 'descartada', 'vencida'))
        or (old.status = 'enviada' and new.status in ('en_revision', 'sin_respuesta_de_meta', 'aprobada_meta',
                                                       'rechazada'))
        or (old.status = 'en_revision' and new.status in ('sin_respuesta_de_meta', 'aprobada_meta', 'rechazada'))
        or (old.status = 'sin_respuesta_de_meta' and new.status in ('aprobada_meta', 'rechazada'))
        or (old.status = 'aprobada_meta' and new.status = 'en_catalogo')
    ) then
        raise exception using errcode = '23514', message = 'meta_template_request_transition_invalid';
    end if;
    if old.status = 'borrador' then
        if new.status = 'borrador' then
            if row(new.draft, new.purpose, new.proposal_id, new.step_pointer)
               is distinct from row(old.draft, old.purpose, old.proposal_id, old.step_pointer)
               and new.revision is distinct from old.revision + 1 then
                raise exception using errcode = '23514', message = 'meta_template_request_revision_must_increment';
            end if;
        elsif row(new.draft, new.purpose, new.proposal_id, new.step_pointer, new.revision)
              is distinct from row(old.draft, old.purpose, old.proposal_id, old.step_pointer, old.revision) then
            raise exception using errcode = '55000', message = 'meta_template_request_frozen';
        end if;
    else
        if row(new.draft, new.purpose, new.proposal_id, new.step_pointer, new.revision, new.template_name,
               new.language, new.category_requested, new.fingerprint, new.validated_at, new.expires_at,
               new.validation)
           is distinct from row(old.draft, old.purpose, old.proposal_id, old.step_pointer, old.revision,
               old.template_name, old.language, old.category_requested, old.fingerprint, old.validated_at,
               old.expires_at, old.validation) then
            raise exception using errcode = '55000', message = 'meta_template_request_frozen';
        end if;
        if old.status not in ('validada')
           and row(new.approved_at, new.approved_by_kind, new.approved_by_ref)
               is distinct from row(old.approved_at, old.approved_by_kind, old.approved_by_ref) then
            raise exception using errcode = '55000', message = 'meta_template_request_frozen';
        end if;
        -- El id de Meta, una vez guardado, no cambia.
        if old.meta_template_id is not null and new.meta_template_id is distinct from old.meta_template_id then
            raise exception using errcode = '55000', message = 'meta_template_request_frozen';
        end if;
        if old.sent_at is not null and new.sent_at is distinct from old.sent_at then
            raise exception using errcode = '55000', message = 'meta_template_request_frozen';
        end if;
    end if;
    return new;
end;
$function$;

create trigger meta_template_requests_validate
before update on public.meta_template_requests
for each row execute function public.validate_meta_template_request_change();
create trigger meta_template_requests_permanent
before delete on public.meta_template_requests
for each row execute function public.reject_meta_template_request_mutation();
create trigger meta_template_requests_no_truncate
before truncate on public.meta_template_requests
for each statement execute function public.reject_meta_template_request_mutation();
create trigger meta_template_requests_set_updated_at
before update on public.meta_template_requests
for each row execute function public.set_updated_at();
create trigger meta_template_request_events_append_only
before update or delete on public.meta_template_request_events
for each row execute function public.reject_meta_template_request_mutation();
create trigger meta_template_request_events_no_truncate
before truncate on public.meta_template_request_events
for each statement execute function public.reject_meta_template_request_mutation();

-- ===========================================================================
-- 4. Los helpers internos (security invoker, de nadie de la API).
-- ===========================================================================

create function public._meta_template_request_event(
    p_request public.meta_template_requests, p_event_type text, p_actor_kind text, p_actor_ref text,
    p_reason_code text, p_data jsonb)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.meta_template_request_events (tenant_ref, ally_ref, request_id, event_type,
        actor_kind, actor_ref, reason_code, data)
    values (p_request.tenant_ref, p_request.ally_ref, p_request.id, p_event_type,
        p_actor_kind, p_actor_ref, p_reason_code, coalesce(p_data, '{}'::jsonb))
$function$;

-- La huella la calcula solo la base, sobre el texto del jsonb, al congelar.
-- Cubre lo que se manda a Meta (meta_request) y para que flujo es.
create function public._meta_template_request_fingerprint(
    p_request_id uuid, p_revision integer, p_draft jsonb, p_meta_request jsonb, p_purpose text,
    p_proposal_id uuid, p_step_pointer text)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select encode(sha256(convert_to(jsonb_build_object(
        'contract', 'template-request-v1', 'request_id', p_request_id, 'revision', p_revision,
        'draft', p_draft, 'meta_request', p_meta_request, 'purpose', p_purpose,
        'proposal_id', p_proposal_id, 'step_pointer', p_step_pointer)::text, 'UTF8')), 'hex')
$function$;

-- Con la solicitud ya bloqueada: si esta validada o aprobada y vencio, la pasa
-- a vencida con sistema/vencimiento y deja su evento. No hay barrido.
create function public._meta_template_request_expire_if_due(p_request public.meta_template_requests)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_request.status not in ('validada', 'aprobada')
       or not coalesce(clock_timestamp() >= p_request.expires_at, false) then
        return false;
    end if;
    update public.meta_template_requests
       set status = 'vencida', closed_at = clock_timestamp(),
           closed_by_kind = 'sistema', closed_by_ref = 'vencimiento'
     where id = p_request.id;
    perform public._meta_template_request_event(p_request, 'meta_template_expired', 'sistema', 'vencimiento',
        'meta_template_request_expired', jsonb_build_object('expires_at', p_request.expires_at,
        'from_status', p_request.status));
    return true;
end;
$function$;

-- Lo que se muestra de una solicitud. Con p_with_people (la pagina), el ID de
-- Slack de quien la aprobo; el agente no lo recibe. Nunca quien la pidio.
create function public._meta_template_request_view(p_request public.meta_template_requests, p_with_people boolean)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'request_id', p_request.id,
        'status', p_request.status,
        'effective_status', case
            when p_request.status in ('validada', 'aprobada') and clock_timestamp() >= p_request.expires_at
            then 'vencida' else p_request.status end,
        'revision', p_request.revision,
        'template_name', p_request.template_name,
        'language', p_request.language,
        'category_requested', p_request.category_requested,
        'draft', p_request.draft,
        'purpose', p_request.purpose,
        'proposal_id', p_request.proposal_id,
        'step_pointer', p_request.step_pointer,
        'validation', p_request.validation,
        'fingerprint', p_request.fingerprint,
        'validated_at', p_request.validated_at,
        'expires_at', p_request.expires_at,
        'approved_at', p_request.approved_at,
        'approved_by_kind', p_request.approved_by_kind,
        'sent_at', p_request.sent_at,
        'send_attempts', p_request.send_attempts,
        'last_send_error', p_request.last_send_error,
        'meta_template_id', p_request.meta_template_id,
        'meta_status', p_request.meta_status,
        'meta_category', p_request.meta_category,
        'recategorized', p_request.meta_category is not null
            and p_request.meta_category is distinct from p_request.category_requested,
        'meta_rejected_reason', p_request.meta_rejected_reason,
        'meta_answered_at', p_request.meta_answered_at,
        'in_catalog_at', p_request.in_catalog_at,
        'closed_at', p_request.closed_at,
        'closed_by_kind', p_request.closed_by_kind,
        'created_at', p_request.created_at,
        'events', coalesce((
            select jsonb_agg(jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                'reason_code', e.reason_code, 'created_at', e.created_at)
                             order by e.created_at desc, e.id)
              from (select * from public.meta_template_request_events e
                     where e.request_id = p_request.id
                     order by e.created_at desc, e.id limit 50) e), '[]'::jsonb))
        || case when p_with_people then jsonb_build_object('approved_by_ref', p_request.approved_by_ref)
                else '{}'::jsonb end
$function$;

-- Las abiertas del agente: un borrador, o una validada o aprobada sin vencer.
create function public._meta_template_requests_open(p_tenant_ref text, p_ally_ref text)
returns integer
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select count(*)::integer from public.meta_template_requests r
     where r.tenant_ref = p_tenant_ref and r.ally_ref = p_ally_ref
       and (r.status = 'borrador'
            or (r.status in ('validada', 'aprobada') and clock_timestamp() < r.expires_at))
$function$;

-- ===========================================================================
-- 5. Las cuatro del Automation Expert.
-- ===========================================================================

-- Guardar o rehacer un borrador, con sus topes. El bridge ya corrio las
-- reglas de forma (template_request.py) y manda el borrador normalizado.
create function public.save_meta_template_request_draft_v1(
    p_instance jsonb, p_request_id uuid, p_expected_revision integer, p_draft jsonb, p_purpose text,
    p_proposal_id uuid, p_step_pointer text, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_request public.meta_template_requests%rowtype;
    v_expired public.meta_template_requests%rowtype;
    v_open integer;
    v_name text;
    v_language text;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_purpose) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    if not coalesce(public._meta_template_draft_is_valid(p_draft), false) then
        raise exception using errcode = '22023', message = 'meta_template_draft_invalid';
    end if;
    if (p_request_id is null) is distinct from (p_expected_revision is null) then
        raise exception using errcode = '22023', message = 'meta_template_redo_invalid';
    end if;
    if (p_proposal_id is null) is distinct from (p_step_pointer is null)
       or not coalesce(p_step_pointer is null or p_step_pointer ~ '^(/[a-z0-9_]{1,40}){1,16}$', false) then
        raise exception using errcode = '22023', message = 'meta_template_flow_link_invalid';
    end if;
    -- La propuesta de flujo, si viene de una, es del agente y de esta instancia.
    if p_proposal_id is not null and not exists (
        select 1 from public.flow_change_proposals p
         where p.id = p_proposal_id and p.tenant_ref = v_instance.o_tenant_ref
           and p.ally_ref = v_instance.o_ally_ref and p.requested_by_kind = 'automation_expert'
    ) then
        raise exception using errcode = 'P0002', message = 'flow_change_not_found';
    end if;
    v_name := p_draft ->> 'nombre';
    v_language := p_draft ->> 'idioma';

    -- Un lock por instancia: dos borradores a la vez no pasan el tope ni
    -- toman el mismo nombre juntos. El lock_timeout va con set_config local.
    perform set_config('lock_timeout', '5s', true);
    perform pg_advisory_xact_lock(hashtextextended(
        'meta_template_requests:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));

    if p_request_id is null then
        v_open := public._meta_template_requests_open(v_instance.o_tenant_ref, v_instance.o_ally_ref);
        if v_open >= 10 then
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_open_limit', 'open', v_open);
        end if;
    else
        select * into v_request from public.meta_template_requests r
         where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref
           and r.ally_ref = v_instance.o_ally_ref
         for update;
        if not found then
            raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
        end if;
        if public._meta_template_request_expire_if_due(v_request) then
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_request_expired');
        end if;
        if v_request.status is distinct from 'borrador' then
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_not_draft',
                'status', v_request.status);
        end if;
        if v_request.revision is distinct from p_expected_revision then
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_revision_mismatch',
                'revision', v_request.revision);
        end if;
        if v_request.revision >= 100 then
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_revision_limit');
        end if;
    end if;

    -- El vencimiento es perezoso: una validada o aprobada con el mismo nombre que ya vencio se marca
    -- ahora, asi no retiene el nombre (ni el indice unico).
    for v_expired in
        select * from public.meta_template_requests r
         where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
           and r.template_name = v_name and r.language = v_language
           and r.status in ('validada', 'aprobada') and r.expires_at <= clock_timestamp()
           and r.id is distinct from p_request_id
         order by r.id
         for update
    loop
        perform public._meta_template_request_expire_if_due(v_expired);
    end loop;
    if exists (
        select 1 from public.meta_template_requests r
         where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
           and r.template_name = v_name and r.language = v_language
           and r.status not in ('descartada', 'vencida')
           and r.id is distinct from p_request_id
    ) then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_name_taken');
    end if;

    if p_request_id is null then
        insert into public.meta_template_requests (tenant_ref, ally_ref, status, revision, template_name,
            language, category_requested, draft, purpose, proposal_id, step_pointer,
            requested_by_kind, requested_by_ref)
        values (v_instance.o_tenant_ref, v_instance.o_ally_ref, 'borrador', 1, v_name, v_language,
            p_draft ->> 'categoria', p_draft, p_purpose, p_proposal_id, p_step_pointer,
            'automation_expert', p_requested_by_ref)
        returning * into v_request;
    else
        update public.meta_template_requests
           set draft = p_draft, template_name = v_name, language = v_language,
               category_requested = p_draft ->> 'categoria', purpose = p_purpose,
               proposal_id = p_proposal_id, step_pointer = p_step_pointer,
               revision = revision + 1, validation = null
         where id = v_request.id
        returning * into v_request;
    end if;

    perform public._meta_template_request_event(v_request, 'meta_template_draft_saved', 'automation_expert',
        p_requested_by_ref, 'meta_template_draft_saved', jsonb_build_object(
            'revision', v_request.revision,
            'template_name', v_request.template_name,
            'language', v_request.language,
            'category_requested', v_request.category_requested,
            'draft_sha256', encode(sha256(convert_to(p_draft::text, 'UTF8')), 'hex'),
            'purpose_sha256', encode(sha256(convert_to(p_purpose, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'request_id', v_request.id, 'revision', v_request.revision,
        'status', v_request.status);
end;
$function$;

-- Registrar la validacion que corrio el bridge. La base no corre las reglas:
-- le cree a quien registra el resultado, pero arma ella la huella. Sin
-- bloqueos, congela la solicitud 24 horas con el pedido de Meta exacto.
create function public.record_meta_template_request_validation_v1(
    p_instance jsonb, p_request_id uuid, p_revision integer, p_validator jsonb, p_result jsonb,
    p_meta_request jsonb, p_preview jsonb, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_request public.meta_template_requests%rowtype;
    v_validations integer;
    v_now timestamptz;
    v_fingerprint text;
    v_validation jsonb;
    v_body text;
    v_index integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_validator) = 'object'
        and (select array_agg(k order by k) from jsonb_object_keys(p_validator) k)
            = array['contract', 'git_sha', 'product_version', 'rules_version']
        and p_validator ->> 'contract' = 'template-request-v1'
        and jsonb_typeof(p_validator -> 'rules_version') = 'number'
        and p_validator ->> 'rules_version' ~ '^[1-9][0-9]{0,3}$'
        and p_validator ->> 'product_version' ~ '^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
        and p_validator ->> 'git_sha' ~ '^[0-9a-f]{7,40}$', false) then
        raise exception using errcode = '22023', message = 'meta_template_validator_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_result) = 'object'
        and (select array_agg(k order by k) from jsonb_object_keys(p_result) k) = array['blocks', 'warnings']
        and jsonb_typeof(p_result -> 'blocks') = 'array' and jsonb_typeof(p_result -> 'warnings') = 'array'
        and jsonb_array_length(p_result -> 'blocks') <= 100 and jsonb_array_length(p_result -> 'warnings') <= 100
        and octet_length(p_result::text) <= 32768
        and not exists (select 1 from jsonb_array_elements(p_result -> 'blocks') f
                         where not coalesce(f ->> 'code' ~ '^[a-z][a-z0-9_]{1,79}$', false))
        and not exists (select 1 from jsonb_array_elements(p_result -> 'warnings') f
                         where not coalesce(f ->> 'code' ~ '^[a-z][a-z0-9_]{1,79}$', false)), false) then
        raise exception using errcode = '22023', message = 'meta_template_result_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_preview) = 'object' and octet_length(p_preview::text) <= 65536, false) then
        raise exception using errcode = '22023', message = 'meta_template_preview_invalid';
    end if;
    if p_revision is null then
        raise exception using errcode = '22023', message = 'meta_template_revision_invalid';
    end if;

    select * into v_request from public.meta_template_requests r
     where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
    end if;
    select count(*) into v_validations from public.meta_template_request_events e
     where e.request_id = v_request.id
       and e.event_type in ('meta_template_validated', 'meta_template_validation_blocked');
    if v_validations >= 100 then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_validation_limit');
    end if;
    if v_request.status is distinct from 'borrador' then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_not_draft', 'status', v_request.status);
    end if;
    if v_request.revision is distinct from p_revision then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_revision_mismatch',
            'revision', v_request.revision);
    end if;

    if jsonb_array_length(p_result -> 'blocks') > 0 then
        v_validation := jsonb_build_object('validator', p_validator, 'result', p_result, 'preview', p_preview);
        update public.meta_template_requests set validation = v_validation where id = v_request.id;
        perform public._meta_template_request_event(v_request, 'meta_template_validation_blocked',
            'automation_expert', p_requested_by_ref, 'meta_template_validation_blocked',
            jsonb_build_object('revision', v_request.revision,
                'codes', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                            from jsonb_array_elements(p_result -> 'blocks') f),
                'validator', p_validator));
        return jsonb_build_object('ok', true, 'status', 'borrador',
            'blocks', jsonb_array_length(p_result -> 'blocks'));
    end if;

    -- Sin bloqueos, el pedido de Meta es obligatorio y tiene que ser de esta fila: su nombre, idioma y
    -- categoria, el cuerpo del borrador numerado en el orden de sus variables, y su encabezado.
    if not coalesce(public._meta_template_meta_request_is_valid(p_meta_request, v_request.template_name,
                        v_request.language, v_request.category_requested), false) then
        raise exception using errcode = '22023', message = 'meta_template_meta_request_invalid';
    end if;
    v_body := v_request.draft ->> 'cuerpo';
    for v_index in 1 .. jsonb_array_length(v_request.draft -> 'variables') loop
        v_body := regexp_replace(v_body,
            '\{\{\s*' || (v_request.draft -> 'variables' ->> (v_index - 1)) || '\s*\}\}',
            '{{' || v_index || '}}', 'g');
    end loop;
    if (select count(*) from jsonb_array_elements(p_meta_request -> 'components') c
         where c ->> 'type' = 'BODY' and c ->> 'text' = v_body) <> 1
       or (select c ->> 'text' from jsonb_array_elements(p_meta_request -> 'components') c
            where c ->> 'type' = 'HEADER') is distinct from v_request.draft ->> 'encabezado' then
        raise exception using errcode = '22023', message = 'meta_template_meta_request_invalid';
    end if;
    v_validation := jsonb_build_object('validator', p_validator, 'result', p_result, 'preview', p_preview,
        'meta_request', p_meta_request);
    v_now := clock_timestamp();
    v_fingerprint := public._meta_template_request_fingerprint(v_request.id, v_request.revision, v_request.draft,
        p_meta_request, v_request.purpose, v_request.proposal_id, v_request.step_pointer);
    update public.meta_template_requests
       set status = 'validada', fingerprint = v_fingerprint, validated_at = v_now,
           expires_at = v_now + interval '24 hours', validation = v_validation
     where id = v_request.id
    returning * into v_request;
    perform public._meta_template_request_event(v_request, 'meta_template_validated', 'automation_expert',
        p_requested_by_ref, 'meta_template_validated', jsonb_build_object('revision', v_request.revision,
            'fingerprint', v_fingerprint, 'validator', p_validator,
            'warnings', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                           from jsonb_array_elements(p_result -> 'warnings') f),
            'expires_at', v_request.expires_at));
    return jsonb_build_object('ok', true, 'status', 'validada', 'fingerprint', v_fingerprint,
        'expires_at', v_request.expires_at);
end;
$function$;

-- Releer una solicitud, sin el ID de Slack de nadie.
create function public.get_meta_template_request_v1(p_instance jsonb, p_request_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_request public.meta_template_requests%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_request from public.meta_template_requests r
     where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
    end if;
    return public._meta_template_request_view(v_request, false);
end;
$function$;

-- Las solicitudes de la instancia, de la mas nueva a la mas vieja, hasta
-- p_limit (1 a 50). Con p_only_open, las abiertas del tope y las que esperan a
-- Meta o al catalogo (lo que el AE le cuenta al duenio cuando vuelve).
create function public.list_meta_template_requests_v1(p_instance jsonb, p_only_open boolean, p_limit integer)
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
    if p_only_open is null or p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'meta_template_list_parameters_invalid';
    end if;
    select coalesce(jsonb_agg(row_data order by created_at desc, request_id), '[]'::jsonb)
      into v_rows
      from (
        select r.created_at, r.id as request_id, jsonb_build_object(
                   'request_id', r.id,
                   'status', r.status,
                   'effective_status', case
                       when r.status in ('validada', 'aprobada') and clock_timestamp() >= r.expires_at
                       then 'vencida' else r.status end,
                   'revision', r.revision,
                   'template_name', r.template_name,
                   'language', r.language,
                   'category_requested', r.category_requested,
                   'purpose', r.purpose,
                   'proposal_id', r.proposal_id,
                   'step_pointer', r.step_pointer,
                   'variables', r.draft -> 'variables',
                   'expires_at', r.expires_at,
                   'approved_at', r.approved_at,
                   'sent_at', r.sent_at,
                   'meta_status', r.meta_status,
                   'meta_category', r.meta_category,
                   'recategorized', r.meta_category is not null
                       and r.meta_category is distinct from r.category_requested,
                   'meta_rejected_reason', r.meta_rejected_reason,
                   'in_catalog_at', r.in_catalog_at,
                   'closed_at', r.closed_at,
                   'created_at', r.created_at,
                   'last_event', (
                       select jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                 'reason_code', e.reason_code, 'created_at', e.created_at)
                         from public.meta_template_request_events e
                        where e.request_id = r.id
                        order by e.created_at desc, e.id desc
                        limit 1)) as row_data
          from public.meta_template_requests r
         where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
           and (not p_only_open
                or r.status in ('borrador', 'enviada', 'en_revision', 'sin_respuesta_de_meta', 'aprobada_meta')
                or (r.status in ('validada', 'aprobada') and clock_timestamp() < r.expires_at))
         order by r.created_at desc, r.id
         limit p_limit) listed;
    return jsonb_build_object('requests', v_rows,
        'open', public._meta_template_requests_open(v_instance.o_tenant_ref, v_instance.o_ally_ref),
        'open_limit', 10, 'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 6. Las cinco de la pagina, con la sesion viva.
-- ===========================================================================

create function public.approver_get_meta_template_request_v1(
    p_instance jsonb, p_session_hash text, p_request_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_request public.meta_template_requests%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_request from public.meta_template_requests r
     where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
    end if;
    return public._meta_template_request_view(v_request, true);
end;
$function$;

-- Aprobar exactamente la solicitud validada: la huella viaja con el si. Un
-- solo paso (D3): mandar una plantilla no le escribe a nadie.
create function public.approver_approve_meta_template_request_v1(
    p_instance jsonb, p_session_hash text, p_request_id uuid, p_fingerprint text)
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
    v_request public.meta_template_requests%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'meta_template_fingerprint_invalid';
    end if;
    select * into v_request from public.meta_template_requests r
     where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
    end if;
    if public._meta_template_request_expire_if_due(v_request) then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_request_expired');
    end if;
    if v_request.fingerprint is not null and p_fingerprint is distinct from v_request.fingerprint then
        raise exception using errcode = '22023', message = 'meta_template_fingerprint_mismatch';
    end if;
    if v_request.status in ('aprobada', 'enviada', 'en_revision', 'sin_respuesta_de_meta', 'aprobada_meta',
                            'rechazada', 'en_catalogo') then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', v_request.status,
            'approved_by_kind', v_request.approved_by_kind, 'approved_by_ref', v_request.approved_by_ref);
    end if;
    if v_request.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_not_validated',
            'status', v_request.status);
    end if;
    update public.meta_template_requests
       set status = 'aprobada', approved_at = clock_timestamp(), approved_by_kind = 'slack',
           approved_by_ref = v_actor
     where id = v_request.id;
    perform public._meta_template_request_event(v_request, 'meta_template_approved', 'slack', v_actor,
        'meta_template_approved', jsonb_build_object('fingerprint', v_request.fingerprint));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'aprobada',
        'approved_by_kind', 'slack', 'approved_by_ref', v_actor);
end;
$function$;

-- Descartar, con un motivo de una linea. Solo lo que no llego a Meta.
create function public.approver_discard_meta_template_request_v1(
    p_instance jsonb, p_session_hash text, p_request_id uuid, p_reason text)
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
    v_request public.meta_template_requests%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_request from public.meta_template_requests r
     where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
    end if;
    if public._meta_template_request_expire_if_due(v_request) then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_request_final', 'status', 'vencida');
    end if;
    if v_request.status = 'descartada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_request.status not in ('borrador', 'validada', 'aprobada') then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_request_final',
            'status', v_request.status);
    end if;
    update public.meta_template_requests
       set status = 'descartada', closed_at = clock_timestamp(), closed_by_kind = 'slack',
           closed_by_ref = v_actor, close_reason = p_reason
     where id = v_request.id;
    perform public._meta_template_request_event(v_request, 'meta_template_discarded', 'slack', v_actor,
        'meta_template_discarded', jsonb_build_object('from_status', v_request.status,
            'close_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

-- Empezar el envio: aprobada pasa a enviada y devuelve el pedido congelado,
-- que es lo unico que el bridge manda a Meta. Con el tope por dia de la
-- instancia (D8, p_daily_cap de 1 a 20, lo pone META_TEMPLATES_DAILY_CAP):
-- cuenta las solicitudes con su primer envio en las ultimas 24 horas. Un
-- envio que quedo a medias (enviada sin respuesta registrada) se puede volver
-- a empezar a los 2 minutos, hasta 5 intentos: el bridge relee la lista de
-- Meta por nombre antes de crear, asi un reintento no duplica.
create function public.approver_begin_meta_template_send_v1(
    p_instance jsonb, p_session_hash text, p_request_id uuid, p_fingerprint text, p_daily_cap integer)
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
    v_request public.meta_template_requests%rowtype;
    v_sent integer;
    v_now timestamptz;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'meta_template_fingerprint_invalid';
    end if;
    if p_daily_cap is null or p_daily_cap < 1 or p_daily_cap > 20 then
        raise exception using errcode = '22023', message = 'meta_template_daily_cap_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'meta_template_requests:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_request from public.meta_template_requests r
     where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
    end if;
    if public._meta_template_request_expire_if_due(v_request) then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_request_expired');
    end if;
    if v_request.fingerprint is not null and p_fingerprint is distinct from v_request.fingerprint then
        raise exception using errcode = '22023', message = 'meta_template_fingerprint_mismatch';
    end if;
    v_now := clock_timestamp();
    if v_request.status = 'enviada' then
        if v_request.send_started_at > v_now - interval '2 minutes' then
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_send_in_progress');
        end if;
        if v_request.send_attempts >= 5 then
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_send_attempts_exhausted');
        end if;
        update public.meta_template_requests
           set send_started_at = v_now, send_attempts = send_attempts + 1
         where id = v_request.id
        returning * into v_request;
    elsif v_request.status = 'aprobada' then
        select count(*) into v_sent from public.meta_template_requests r
         where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
           and r.sent_at > v_now - interval '24 hours';
        if v_sent >= p_daily_cap then
            perform public._meta_template_request_event(v_request, 'meta_template_send_refused', 'slack', v_actor,
                'meta_template_daily_cap', jsonb_build_object('sent_last_24h', v_sent, 'daily_cap', p_daily_cap));
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_daily_cap',
                'sent_last_24h', v_sent, 'daily_cap', p_daily_cap);
        end if;
        update public.meta_template_requests
           set status = 'enviada', sent_at = v_now, send_started_at = v_now, send_attempts = 1
         where id = v_request.id
        returning * into v_request;
    else
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_not_approved',
            'status', v_request.status);
    end if;
    perform public._meta_template_request_event(v_request, 'meta_template_send_started', 'slack', v_actor,
        'meta_template_send_started', jsonb_build_object('attempt', v_request.send_attempts));
    return jsonb_build_object('ok', true, 'status', 'enviada', 'attempt', v_request.send_attempts,
        'template_name', v_request.template_name, 'language', v_request.language,
        'meta_request', v_request.validation -> 'meta_request');
end;
$function$;

-- Lo que contesto Meta al crearla (POST /{WABA}/message_templates: id, status
-- y category), o el codigo del error si no se pudo. Con error queda enviada,
-- y se puede volver a empezar.
create function public.approver_record_meta_template_sent_v1(
    p_instance jsonb, p_session_hash text, p_request_id uuid, p_meta_template_id text, p_meta_status text,
    p_meta_category text, p_error_code text)
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
    v_request public.meta_template_requests%rowtype;
    v_status text;
    v_now timestamptz;
    v_recategorized boolean;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if p_error_code is not null then
        if not coalesce(p_error_code ~ '^[a-z][a-z0-9_:]{0,79}$', false)
           or p_meta_template_id is not null or p_meta_status is not null or p_meta_category is not null then
            raise exception using errcode = '22023', message = 'meta_template_send_result_invalid';
        end if;
    elsif not coalesce(p_meta_template_id ~ '^[0-9]{1,32}$'
                       and p_meta_status in ('PENDING', 'IN_APPEAL', 'APPROVED', 'REJECTED')
                       and p_meta_category in ('UTILITY', 'MARKETING', 'AUTHENTICATION'), false) then
        raise exception using errcode = '22023', message = 'meta_template_send_result_invalid';
    end if;
    select * into v_request from public.meta_template_requests r
     where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
    end if;
    if v_request.meta_template_id is not null then
        if p_meta_template_id is distinct from v_request.meta_template_id then
            return jsonb_build_object('ok', false, 'reason_code', 'meta_template_already_sent',
                'status', v_request.status);
        end if;
        return jsonb_build_object('ok', true, 'replayed', true, 'status', v_request.status);
    end if;
    if v_request.status is distinct from 'enviada' then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_not_sending',
            'status', v_request.status);
    end if;
    if p_error_code is not null then
        update public.meta_template_requests set last_send_error = p_error_code where id = v_request.id;
        perform public._meta_template_request_event(v_request, 'meta_template_send_failed', 'slack', v_actor,
            p_error_code, jsonb_build_object('attempt', v_request.send_attempts));
        return jsonb_build_object('ok', true, 'status', 'enviada', 'send_failed', true);
    end if;
    v_now := clock_timestamp();
    v_status := case p_meta_status when 'APPROVED' then 'aprobada_meta' when 'REJECTED' then 'rechazada'
                     else 'en_revision' end;
    v_recategorized := p_meta_category is distinct from v_request.category_requested;
    update public.meta_template_requests
       set status = v_status, meta_template_id = p_meta_template_id, meta_status = p_meta_status,
           meta_category = p_meta_category, last_send_error = null,
           meta_answered_at = case when v_status = 'en_revision' then null else v_now end,
           closed_at = case when v_status = 'rechazada' then v_now end,
           closed_by_kind = case when v_status = 'rechazada' then 'sistema' end,
           closed_by_ref = case when v_status = 'rechazada' then 'meta' end
     where id = v_request.id;
    perform public._meta_template_request_event(v_request, 'meta_template_sent', 'slack', v_actor,
        'meta_template_sent', jsonb_build_object('meta_template_id', p_meta_template_id,
            'meta_status', p_meta_status, 'meta_category', p_meta_category,
            'recategorized', v_recategorized, 'attempt', v_request.send_attempts));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', v_status,
        'recategorized', v_recategorized);
end;
$function$;

-- ===========================================================================
-- 7. La de service_role: el estado que leyo el sondeo (D2).
-- ===========================================================================

-- p_meta_status es lo que contesto GET /{template_id}; p_in_catalog, si la
-- plantilla ya esta APPROVED en el catalogo de la bandeja de Chatwoot (lo que
-- usa el bridge para mandar). Con PENDING o IN_APPEAL a las 48 horas del
-- primer envio pasa a sin_respuesta_de_meta. Un estado que no es de la
-- revision (PAUSED, DISABLED...) se anota y no mueve la solicitud: lo cubren
-- el catalogo, que se relee en cada envio, y la salud de la cuenta (F-139).
-- p_meta_template_id es el de la fila, o el que encontro el sondeo por nombre
-- si el envio quedo a medias (enviada sin id).
create function public.record_meta_template_status_v1(
    p_instance jsonb, p_request_id uuid, p_meta_template_id text, p_meta_status text, p_meta_category text,
    p_rejected_reason text, p_in_catalog boolean)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_request public.meta_template_requests%rowtype;
    v_now timestamptz;
    v_status text;
    v_before_status text;
    v_before_meta_status text;
    v_event text;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_meta_template_id ~ '^[0-9]{1,32}$' and p_meta_status ~ '^[A-Z][A-Z_]{1,31}$'
                    and (p_meta_category is null or p_meta_category in ('UTILITY', 'MARKETING', 'AUTHENTICATION'))
                    and (p_rejected_reason is null or p_rejected_reason ~ '^[A-Z][A-Z_]{1,63}$')
                    and p_in_catalog is not null
                    and (not p_in_catalog or p_meta_status = 'APPROVED')
                    and (p_meta_status <> 'APPROVED' or p_meta_category is not null), false) then
        raise exception using errcode = '22023', message = 'meta_template_status_invalid';
    end if;
    select * into v_request from public.meta_template_requests r
     where r.id = p_request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_template_request_not_found';
    end if;
    if v_request.status in ('rechazada', 'en_catalogo', 'vencida', 'descartada') then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_request_final',
            'status', v_request.status);
    end if;
    if v_request.status not in ('enviada', 'en_revision', 'sin_respuesta_de_meta', 'aprobada_meta') then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_not_sent', 'status', v_request.status);
    end if;
    if v_request.meta_template_id is not null and p_meta_template_id is distinct from v_request.meta_template_id then
        raise exception using errcode = '22023', message = 'meta_template_id_mismatch';
    end if;
    v_now := clock_timestamp();
    v_before_status := v_request.status;
    v_before_meta_status := v_request.meta_status;
    v_status := v_request.status;
    if p_meta_status = 'APPROVED' then
        v_status := case when p_in_catalog then 'en_catalogo' else 'aprobada_meta' end;
    elsif p_meta_status = 'REJECTED' and v_request.status <> 'aprobada_meta' then
        v_status := 'rechazada';
    elsif p_meta_status in ('PENDING', 'IN_APPEAL') and v_request.status in ('enviada', 'en_revision') then
        v_status := case when v_request.sent_at <= v_now - interval '48 hours' then 'sin_respuesta_de_meta'
                         else 'en_revision' end;
    end if;
    -- Una enviada sin id que el sondeo encontro por nombre (con un estado que no es de la revision, como
    -- PAUSED) ya esta en Meta: pasa a la revision con su id, que es lo que pide el CHECK.
    if v_status = 'enviada' then
        v_status := case when v_request.sent_at <= v_now - interval '48 hours' then 'sin_respuesta_de_meta'
                         else 'en_revision' end;
    end if;

    -- De enviada o de la revision a en_catalogo se pasa por aprobada_meta, que
    -- es lo que pide el recorrido, y queda el evento de la aprobacion.
    if v_status = 'en_catalogo' and v_request.status <> 'aprobada_meta' then
        update public.meta_template_requests
           set status = 'aprobada_meta', meta_template_id = p_meta_template_id, meta_status = p_meta_status,
               meta_category = p_meta_category, meta_answered_at = v_now, last_polled_at = v_now
         where id = v_request.id
        returning * into v_request;
        perform public._meta_template_request_event(v_request, 'meta_template_approved_by_meta', 'sistema',
            'sondeo', 'meta_template_approved_by_meta', jsonb_build_object('meta_status', p_meta_status,
                'meta_category', p_meta_category,
                'recategorized', p_meta_category is distinct from v_request.category_requested));
    end if;

    update public.meta_template_requests
       set status = v_status, meta_template_id = p_meta_template_id, meta_status = p_meta_status,
           meta_category = coalesce(p_meta_category, meta_category),
           meta_rejected_reason = case when v_status = 'rechazada' then p_rejected_reason
                                       else meta_rejected_reason end,
           meta_answered_at = case when v_status in ('aprobada_meta', 'rechazada', 'en_catalogo')
                                   then coalesce(meta_answered_at, v_now) else meta_answered_at end,
           in_catalog_at = case when v_status = 'en_catalogo' then v_now else in_catalog_at end,
           last_polled_at = v_now,
           closed_at = case when v_status in ('rechazada', 'en_catalogo') then v_now end,
           closed_by_kind = case when v_status in ('rechazada', 'en_catalogo') then 'sistema' end,
           closed_by_ref = case when v_status in ('rechazada', 'en_catalogo') then 'meta' end
     where id = v_request.id
    returning * into v_request;

    -- El mismo estado de antes no deja evento: el sondeo pasa cada pocos
    -- minutos. Un estado de Meta que no mueve la solicitud, uno si cambio.
    v_event := case
        when v_status = v_before_status and p_meta_status is not distinct from v_before_meta_status then null
        when v_status = 'en_catalogo' then 'meta_template_in_catalog'
        when v_status = 'aprobada_meta' and v_before_status <> 'aprobada_meta' then 'meta_template_approved_by_meta'
        when v_status = 'rechazada' then 'meta_template_rejected_by_meta'
        when v_status = 'sin_respuesta_de_meta' and v_before_status <> 'sin_respuesta_de_meta'
            then 'meta_template_no_answer'
        when v_status = 'en_revision' and v_before_status = 'enviada' then 'meta_template_in_review'
        else 'meta_template_status_other' end;
    if v_event is not null then
        perform public._meta_template_request_event(v_request, v_event, 'sistema', 'sondeo', v_event,
            jsonb_build_object('meta_status', p_meta_status, 'meta_category', v_request.meta_category,
                'recategorized', v_request.meta_category is not null
                    and v_request.meta_category is distinct from v_request.category_requested,
                'meta_rejected_reason', p_rejected_reason));
    end if;
    return jsonb_build_object('ok', true, 'status', v_status,
        'recategorized', v_request.meta_category is not null
            and v_request.meta_category is distinct from v_request.category_requested);
end;
$function$;

-- ===========================================================================
-- 8. Los permisos. Las tablas, para nadie de la API. Las internas y los
--    disparadores, para nadie. Cada funcion publica, solo para su rol.
-- ===========================================================================

do $acl$
declare
    v_table text;
    v_function regprocedure;
    v_role text;
begin
    foreach v_table in array array['meta_template_requests', 'meta_template_request_events'] loop
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
        'public._meta_template_draft_is_valid(jsonb)',
        'public._meta_template_meta_request_is_valid(jsonb,text,text,text)',
        'public.reject_meta_template_request_mutation()',
        'public.validate_meta_template_request_change()',
        'public._meta_template_request_event(public.meta_template_requests,text,text,text,text,jsonb)',
        'public._meta_template_request_fingerprint(uuid,integer,jsonb,jsonb,text,uuid,text)',
        'public._meta_template_request_expire_if_due(public.meta_template_requests)',
        'public._meta_template_request_view(public.meta_template_requests,boolean)',
        'public._meta_template_requests_open(text,text)',
        'public.save_meta_template_request_draft_v1(jsonb,uuid,integer,jsonb,text,uuid,text,text)',
        'public.record_meta_template_request_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)',
        'public.get_meta_template_request_v1(jsonb,uuid)',
        'public.list_meta_template_requests_v1(jsonb,boolean,integer)',
        'public.approver_get_meta_template_request_v1(jsonb,text,uuid)',
        'public.approver_approve_meta_template_request_v1(jsonb,text,uuid,text)',
        'public.approver_discard_meta_template_request_v1(jsonb,text,uuid,text)',
        'public.approver_begin_meta_template_send_v1(jsonb,text,uuid,text,integer)',
        'public.approver_record_meta_template_sent_v1(jsonb,text,uuid,text,text,text,text)',
        'public.record_meta_template_status_v1(jsonb,uuid,text,text,text,text,boolean)'
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
    -- Los roles los crean 20261021000100 y 20261028000100; una base sin ellos
    -- no los nombra.
    if to_regrole('automation_expert_change') is not null then
        foreach v_function in array array[
            'public.save_meta_template_request_draft_v1(jsonb,uuid,integer,jsonb,text,uuid,text,text)',
            'public.record_meta_template_request_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)',
            'public.get_meta_template_request_v1(jsonb,uuid)',
            'public.list_meta_template_requests_v1(jsonb,boolean,integer)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to automation_expert_change', v_function);
        end loop;
    end if;
    if to_regrole('flow_change_approver') is not null then
        foreach v_function in array array[
            'public.approver_get_meta_template_request_v1(jsonb,text,uuid)',
            'public.approver_approve_meta_template_request_v1(jsonb,text,uuid,text)',
            'public.approver_discard_meta_template_request_v1(jsonb,text,uuid,text)',
            'public.approver_begin_meta_template_send_v1(jsonb,text,uuid,text,integer)',
            'public.approver_record_meta_template_sent_v1(jsonb,text,uuid,text,text,text,text)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end loop;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.record_meta_template_status_v1(jsonb, uuid, text, text, text, text, boolean)
            to service_role;
    end if;
end;
$acl$;

commit;
