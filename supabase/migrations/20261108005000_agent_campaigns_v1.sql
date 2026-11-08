-- agent-campaigns-v1: la campana del Automation Expert, un segmento que nace
-- como dato y no como archivo (E-355, opcion B del alcance del 2026-11-08).
-- Contrato: docs/contracts/agent-campaigns-v1.md.
--
-- El AE arma la campana con el duenio y la deja como borrador; la valida (corre
-- el bridge) y queda congelada con su huella por 24 horas. El control de
-- calidad (el juez) lo corre el operador por comando y anota el resultado en la
-- fila. Una persona de la lista de aprobadores, desde la pagina de aprobacion,
-- la publica: sin ese si la campana no llega a nadie. Publicada, el bridge la
-- lee de la base (catalogo vivo, con cache corta) y no hay que subir nada al
-- servidor. Se pausa y se reanuda desde la misma pagina.
--
-- 1. Dos tablas nuevas, vacias: agent_campaigns (una fila por campana y
--    version) y agent_campaign_events (solo agregar). Sin permisos para nadie
--    de la API, con RLS.
-- 2. Cuatro funciones del Automation Expert, solo para automation_expert_change
--    (20261021000100): guardar o rehacer un borrador, registrar la validacion,
--    releer una campana y listarlas. Ni publicar, ni pausar, ni descartar.
-- 3. Cinco funciones de la pagina, solo para flow_change_approver
--    (20261028000100), todas con la sesion viva: el actor slack sale de la
--    fila de la sesion, nunca de un parametro. Releer, publicar, pausar,
--    reanudar y descartar.
-- 4. Tres para service_role: la lectura del catalogo vivo, la lectura que hace
--    el comando del juez y el registro de su resultado.
--
-- Una sola campana viva por instancia (decision de Dan del 2026-11-08): un
-- indice unico parcial sobre status = 'publicada', y publicar otra con una viva
-- contesta agent_campaign_live_exists. Una viva cuya vigencia ya termino se
-- marca terminada al publicar la siguiente: no retiene el lugar.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- AGENT_CAMPAIGNS_ENABLED el bridge no llama a ninguna de estas funciones.
-- Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 28000 sesion invalida
-- (flow_approval_session_invalid); P0002 no existe o no es de esta instancia;
-- 55000 un estado que no lo permite. Los resultados del ciclo (revision vieja,
-- vencida, tope, ya hay una viva...) vuelven como {"ok": false, "reason_code"}.

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
        raise exception using errcode = '55000', message = 'agent_campaigns_prerequisite_missing';
    end if;
    if to_regclass('public.agent_campaigns') is not null then
        raise exception using errcode = '55000', message = 'agent_campaigns_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. La forma que usan los CHECK.
-- ===========================================================================

-- El borrador normalizado que guarda el bridge (agent_campaigns.py). La base no
-- corre las reglas: mira la forma, para que una fila no pueda traer otra cosa.
create function public._agent_campaign_draft_is_valid(p_draft jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
declare
    v_keys text[];
begin
    if jsonb_typeof(p_draft) is distinct from 'object' or octet_length(p_draft::text) > 32768 then
        return false;
    end if;
    select array_agg(k order by k) into v_keys from jsonb_object_keys(p_draft) k;
    if not v_keys <@ array['asesoramiento', 'clave', 'desde', 'etiqueta', 'faq', 'hasta', 'instruccion',
                           'macro_etiqueta', 'no_confirmado', 'nombre', 'oferta', 'palabras_clave',
                           'pregunta_entrada', 'preguntas_del_alta', 'promesas_prohibidas', 'se_puede_decir']
       or not v_keys @> array['asesoramiento', 'clave', 'desde', 'etiqueta', 'hasta', 'macro_etiqueta', 'nombre'] then
        return false;
    end if;
    return coalesce(
        jsonb_typeof(p_draft -> 'clave') = 'string'
        and p_draft ->> 'clave' ~ '^[a-z0-9][a-z0-9-]{0,127}$'
        and jsonb_typeof(p_draft -> 'nombre') = 'string'
        and length(p_draft ->> 'nombre') between 1 and 120
        and jsonb_typeof(p_draft -> 'etiqueta') = 'string'
        and p_draft ->> 'etiqueta' ~ '^[a-z0-9][a-z0-9_-]{0,39}$'
        and jsonb_typeof(p_draft -> 'macro_etiqueta') = 'number'
        and (p_draft ->> 'macro_etiqueta') ~ '^[1-9][0-9]{0,11}$'
        and jsonb_typeof(p_draft -> 'desde') = 'string'
        and p_draft ->> 'desde' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$'
        and jsonb_typeof(p_draft -> 'hasta') = 'string'
        and p_draft ->> 'hasta' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$'
        and jsonb_typeof(p_draft -> 'asesoramiento') = 'array'
        and jsonb_array_length(p_draft -> 'asesoramiento') between 1 and 100
        -- Sin palabra clave y sin pregunta no entraria nadie.
        and (p_draft ? 'palabras_clave' or p_draft ? 'pregunta_entrada')
        and (not p_draft ? 'palabras_clave' or (jsonb_typeof(p_draft -> 'palabras_clave') = 'array'
             and jsonb_array_length(p_draft -> 'palabras_clave') between 1 and 8))
        and (not p_draft ? 'pregunta_entrada' or (jsonb_typeof(p_draft -> 'pregunta_entrada') = 'string'
             and length(p_draft ->> 'pregunta_entrada') between 1 and 200))
        and (not p_draft ? 'oferta' or jsonb_typeof(p_draft -> 'oferta') = 'object'), false);
end;
$function$;

-- ===========================================================================
-- 2. Las tablas.
-- ===========================================================================

create table public.agent_campaigns (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    status text not null check (status in (
        'borrador', 'validada', 'publicada', 'pausada', 'terminada', 'vencida', 'descartada')),
    revision integer not null check (revision > 0),
    campaign_key text not null check (campaign_key ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    -- La version del segmento: la clave y la version nombran el bloque en el turno.
    segment_version integer not null check (segment_version > 0),
    label text not null check (label ~ '^[a-z0-9][a-z0-9_-]{0,39}$'),
    label_macro_id bigint not null check (label_macro_id > 0),
    draft jsonb not null check (public._agent_campaign_draft_is_valid(draft)),
    purpose text not null check (public._flow_store_free_text_is_valid(purpose)),
    valid_from timestamptz not null,
    valid_until timestamptz not null,
    requested_by_kind text not null check (requested_by_kind = 'automation_expert'),
    requested_by_ref text not null,
    validation jsonb check (validation is null or (jsonb_typeof(validation) = 'object'
        and octet_length(validation::text) <= 131072)),
    fingerprint text check (fingerprint ~ '^[0-9a-f]{64}$'),
    validated_at timestamptz,
    expires_at timestamptz,
    -- El control de calidad (el juez, por comando del operador).
    check_sha text check (check_sha ~ '^[0-9a-f]{64}$'),
    check_report jsonb check (check_report is null or (jsonb_typeof(check_report) = 'object'
        and octet_length(check_report::text) <= 131072)),
    checked_at timestamptz,
    approved_at timestamptz,
    approved_by_kind text check (approved_by_kind = 'slack'),
    approved_by_ref text,
    paused_at timestamptz,
    paused_by_ref text,
    pause_reason text check (pause_reason is null or public._flow_store_free_text_is_valid(pause_reason)),
    closed_at timestamptz,
    closed_by_kind text check (closed_by_kind in ('slack', 'sistema')),
    closed_by_ref text,
    close_reason text check (close_reason is null or public._flow_store_free_text_is_valid(close_reason)),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    constraint agent_campaigns_window check (valid_from < valid_until),
    constraint agent_campaigns_requested_by
        check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    constraint agent_campaigns_approved_by
        check ((approved_by_kind is null and approved_by_ref is null)
               or public._flow_store_actor_is_valid(approved_by_kind, approved_by_ref)),
    constraint agent_campaigns_paused_by
        check (paused_by_ref is null or public._flow_store_actor_is_valid('slack', paused_by_ref)),
    constraint agent_campaigns_closed_by
        check ((closed_by_kind is null and closed_by_ref is null)
               or public._flow_store_actor_is_valid(closed_by_kind, closed_by_ref)),
    constraint agent_campaigns_identity_matches_draft
        check (draft ->> 'clave' = campaign_key and draft ->> 'etiqueta' = label
               and (draft ->> 'macro_etiqueta')::bigint = label_macro_id),
    -- El CASE entero adentro de coalesce: una comparacion con NULL no deja pasar la fila.
    constraint agent_campaigns_state_coherent check (coalesce(case status
        when 'borrador' then fingerprint is null and validated_at is null and expires_at is null
            and check_sha is null and check_report is null and checked_at is null
            and approved_at is null and approved_by_kind is null and paused_at is null and closed_at is null
        when 'validada' then fingerprint is not null and validation is not null and validated_at is not null
            and expires_at > validated_at and approved_at is null and paused_at is null and closed_at is null
        when 'publicada' then fingerprint is not null and check_sha is not null and approved_at is not null
            and approved_by_kind is not null and paused_at is null and closed_at is null
        when 'pausada' then fingerprint is not null and check_sha is not null and approved_at is not null
            and approved_by_kind is not null and paused_at is not null and paused_by_ref is not null
            and closed_at is null
        when 'terminada' then approved_at is not null and closed_at is not null and closed_by_kind = 'sistema'
        when 'vencida' then fingerprint is not null and expires_at is not null and approved_at is null
            and closed_at is not null and closed_by_kind = 'sistema'
        when 'descartada' then approved_at is null and closed_at is not null and closed_by_kind is not null
            and close_reason is not null
    end, false))
);

-- El id es un uuid y no un identity: una secuencia recibiria permisos por los
-- privilegios por defecto de Supabase.
create table public.agent_campaign_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    campaign_id uuid not null references public.agent_campaigns (id) on delete restrict,
    event_type text not null check (event_type in (
        'agent_campaign_draft_saved', 'agent_campaign_validation_blocked', 'agent_campaign_validated',
        'agent_campaign_checked', 'agent_campaign_published', 'agent_campaign_publish_refused',
        'agent_campaign_paused', 'agent_campaign_resumed', 'agent_campaign_discarded',
        'agent_campaign_expired', 'agent_campaign_finished')),
    actor_kind text not null check (actor_kind in ('automation_expert', 'slack', 'operador', 'sistema')),
    actor_ref text not null,
    reason_code text not null check (reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'),
    data jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object'
        and octet_length(data::text) <= 16384),
    created_at timestamptz not null default clock_timestamp(),
    constraint agent_campaign_events_actor
        check (public._flow_store_actor_is_valid(actor_kind, actor_ref))
);

-- **Una sola viva por instancia.** Aunque el bridge lo mire antes, esto es lo que lo hace cumplir.
create unique index agent_campaigns_one_live
    on public.agent_campaigns (tenant_ref, ally_ref) where status = 'publicada';
-- Una clave con su version nombra un solo bloque.
create unique index agent_campaigns_key_version_once
    on public.agent_campaigns (tenant_ref, ally_ref, campaign_key, segment_version);
create index agent_campaigns_instance_time_idx
    on public.agent_campaigns (tenant_ref, ally_ref, created_at desc);
create index agent_campaigns_open_idx on public.agent_campaigns (tenant_ref, ally_ref)
    where status in ('borrador', 'validada');
create index agent_campaign_events_campaign_time_idx
    on public.agent_campaign_events (campaign_id, created_at);

alter table public.agent_campaigns enable row level security;
alter table public.agent_campaign_events enable row level security;

-- ===========================================================================
-- 3. Los disparadores.
-- ===========================================================================

create function public.reject_agent_campaign_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'agent_campaign_events' then 'agent_campaign_event_is_append_only'
        else 'agent_campaign_is_permanent'
    end;
end;
$function$;

-- El recorrido. Desde validada el contenido queda congelado (volver a borrador
-- es rehacerla, con otra revision); desde publicada, tambien la aprobacion; un
-- estado final no acepta ningun UPDATE.
create function public.validate_agent_campaign_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if old.status in ('terminada', 'vencida', 'descartada') then
        raise exception using errcode = '55000', message = 'agent_campaign_frozen';
    end if;
    if row(new.id, new.tenant_ref, new.ally_ref, new.campaign_key, new.segment_version, new.requested_by_kind,
           new.requested_by_ref, new.created_at)
       is distinct from row(old.id, old.tenant_ref, old.ally_ref, old.campaign_key, old.segment_version,
                            old.requested_by_kind, old.requested_by_ref, old.created_at) then
        raise exception using errcode = '55000', message = 'agent_campaign_identity_is_immutable';
    end if;
    if new.status is distinct from old.status and not (
        (old.status = 'borrador' and new.status in ('validada', 'descartada'))
        or (old.status = 'validada' and new.status in ('borrador', 'publicada', 'descartada', 'vencida'))
        or (old.status = 'publicada' and new.status in ('pausada', 'terminada'))
        or (old.status = 'pausada' and new.status in ('publicada', 'terminada'))
    ) then
        raise exception using errcode = '23514', message = 'agent_campaign_transition_invalid';
    end if;
    if old.status = 'borrador' or (old.status = 'validada' and new.status = 'borrador') then
        if new.status = 'borrador'
           and row(new.draft, new.purpose) is distinct from row(old.draft, old.purpose)
           and new.revision is distinct from old.revision + 1 then
            raise exception using errcode = '23514', message = 'agent_campaign_revision_must_increment';
        end if;
        if old.status = 'borrador' and new.status <> 'borrador'
           and row(new.draft, new.purpose, new.revision)
               is distinct from row(old.draft, old.purpose, old.revision) then
            raise exception using errcode = '55000', message = 'agent_campaign_frozen';
        end if;
    else
        if row(new.draft, new.purpose, new.revision, new.label, new.label_macro_id, new.valid_from, new.valid_until,
               new.fingerprint, new.validated_at, new.validation)
           is distinct from row(old.draft, old.purpose, old.revision, old.label, old.label_macro_id, old.valid_from,
               old.valid_until, old.fingerprint, old.validated_at, old.validation) then
            raise exception using errcode = '55000', message = 'agent_campaign_frozen';
        end if;
        if old.status in ('publicada', 'pausada')
           and row(new.approved_at, new.approved_by_kind, new.approved_by_ref)
               is distinct from row(old.approved_at, old.approved_by_kind, old.approved_by_ref) then
            raise exception using errcode = '55000', message = 'agent_campaign_frozen';
        end if;
    end if;
    return new;
end;
$function$;

create trigger agent_campaigns_validate
before update on public.agent_campaigns
for each row execute function public.validate_agent_campaign_change();
create trigger agent_campaigns_permanent
before delete on public.agent_campaigns
for each row execute function public.reject_agent_campaign_mutation();
create trigger agent_campaigns_no_truncate
before truncate on public.agent_campaigns
for each statement execute function public.reject_agent_campaign_mutation();
create trigger agent_campaigns_set_updated_at
before update on public.agent_campaigns
for each row execute function public.set_updated_at();
create trigger agent_campaign_events_append_only
before update or delete on public.agent_campaign_events
for each row execute function public.reject_agent_campaign_mutation();
create trigger agent_campaign_events_no_truncate
before truncate on public.agent_campaign_events
for each statement execute function public.reject_agent_campaign_mutation();

-- ===========================================================================
-- 4. Los helpers internos (security invoker, de nadie de la API).
-- ===========================================================================

create function public._agent_campaign_event(
    p_campaign public.agent_campaigns, p_event_type text, p_actor_kind text, p_actor_ref text,
    p_reason_code text, p_data jsonb)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.agent_campaign_events (tenant_ref, ally_ref, campaign_id, event_type,
        actor_kind, actor_ref, reason_code, data)
    values (p_campaign.tenant_ref, p_campaign.ally_ref, p_campaign.id, p_event_type,
        p_actor_kind, p_actor_ref, p_reason_code, coalesce(p_data, '{}'::jsonb))
$function$;

-- La huella la calcula solo la base, sobre el texto del jsonb, al congelar.
create function public._agent_campaign_fingerprint(
    p_campaign_id uuid, p_revision integer, p_segment_version integer, p_draft jsonb, p_purpose text)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select encode(sha256(convert_to(jsonb_build_object(
        'contract', 'agent-campaigns-v1', 'campaign_id', p_campaign_id, 'revision', p_revision,
        'segment_version', p_segment_version, 'draft', p_draft, 'purpose', p_purpose)::text, 'UTF8')), 'hex')
$function$;

-- Con la fila ya bloqueada: si esta validada y vencio, la pasa a vencida con
-- sistema/vencimiento y deja su evento. No hay barrido.
create function public._agent_campaign_expire_if_due(p_campaign public.agent_campaigns)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_campaign.status is distinct from 'validada'
       or not coalesce(clock_timestamp() >= p_campaign.expires_at, false) then
        return false;
    end if;
    update public.agent_campaigns
       set status = 'vencida', closed_at = clock_timestamp(),
           closed_by_kind = 'sistema', closed_by_ref = 'vencimiento'
     where id = p_campaign.id;
    perform public._agent_campaign_event(p_campaign, 'agent_campaign_expired', 'sistema', 'vencimiento',
        'agent_campaign_expired', jsonb_build_object('expires_at', p_campaign.expires_at));
    return true;
end;
$function$;

-- Lo que se muestra de una campana. Con p_with_people (la pagina), el ID de
-- Slack de quien la aprobo; el agente no lo recibe. Nunca quien la pidio.
create function public._agent_campaign_view(p_campaign public.agent_campaigns, p_with_people boolean)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'campaign_id', p_campaign.id,
        'status', p_campaign.status,
        'effective_status', case
            when p_campaign.status = 'validada' and clock_timestamp() >= p_campaign.expires_at then 'vencida'
            when p_campaign.status in ('publicada', 'pausada') and clock_timestamp() >= p_campaign.valid_until
                then 'terminada'
            else p_campaign.status end,
        'revision', p_campaign.revision,
        'campaign_key', p_campaign.campaign_key,
        'segment_version', p_campaign.segment_version,
        'label', p_campaign.label,
        'draft', p_campaign.draft,
        'purpose', p_campaign.purpose,
        'valid_from', p_campaign.valid_from,
        'valid_until', p_campaign.valid_until,
        'validation', p_campaign.validation,
        'fingerprint', p_campaign.fingerprint,
        'validated_at', p_campaign.validated_at,
        'expires_at', p_campaign.expires_at,
        'check_sha', p_campaign.check_sha,
        'check_report', p_campaign.check_report,
        'checked_at', p_campaign.checked_at,
        'approved_at', p_campaign.approved_at,
        'approved_by_kind', p_campaign.approved_by_kind,
        'paused_at', p_campaign.paused_at,
        'closed_at', p_campaign.closed_at,
        'closed_by_kind', p_campaign.closed_by_kind,
        'created_at', p_campaign.created_at,
        'events', coalesce((
            select jsonb_agg(jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                'reason_code', e.reason_code, 'created_at', e.created_at)
                             order by e.created_at desc, e.id)
              from (select * from public.agent_campaign_events e
                     where e.campaign_id = p_campaign.id
                     order by e.created_at desc, e.id limit 50) e), '[]'::jsonb))
        || case when p_with_people then jsonb_build_object('approved_by_ref', p_campaign.approved_by_ref,
                                                           'paused_by_ref', p_campaign.paused_by_ref)
                else '{}'::jsonb end
$function$;

-- Las abiertas del agente: un borrador o una validada sin vencer.
create function public._agent_campaigns_open(p_tenant_ref text, p_ally_ref text)
returns integer
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select count(*)::integer from public.agent_campaigns c
     where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
       and (c.status = 'borrador' or (c.status = 'validada' and clock_timestamp() < c.expires_at))
$function$;

-- ===========================================================================
-- 5. Las cuatro del Automation Expert.
-- ===========================================================================

-- Guardar o rehacer un borrador. Rehacer tambien vale sobre una validada que
-- todavia nadie publico: es lo que hace el agente cuando el control de calidad
-- deja preguntas abiertas. La huella cambia, asi que una pagina abierta antes
-- ya no puede publicarla.
create function public.save_agent_campaign_draft_v1(
    p_instance jsonb, p_campaign_id uuid, p_expected_revision integer, p_draft jsonb, p_purpose text,
    p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_campaign public.agent_campaigns%rowtype;
    v_open integer;
    v_version integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_purpose) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    if not coalesce(public._agent_campaign_draft_is_valid(p_draft), false) then
        raise exception using errcode = '22023', message = 'agent_campaign_draft_invalid';
    end if;
    if (p_campaign_id is null) is distinct from (p_expected_revision is null) then
        raise exception using errcode = '22023', message = 'agent_campaign_redo_invalid';
    end if;

    -- Un lock por instancia: dos borradores a la vez no pasan el tope ni toman la misma version.
    perform set_config('lock_timeout', '5s', true);
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_campaigns:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));

    if p_campaign_id is null then
        v_open := public._agent_campaigns_open(v_instance.o_tenant_ref, v_instance.o_ally_ref);
        if v_open >= 5 then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_open_limit', 'open', v_open);
        end if;
        select coalesce(max(c.segment_version), 0) + 1 into v_version from public.agent_campaigns c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and c.campaign_key = p_draft ->> 'clave';
        insert into public.agent_campaigns (tenant_ref, ally_ref, status, revision, campaign_key, segment_version,
            label, label_macro_id, draft, purpose, valid_from, valid_until, requested_by_kind, requested_by_ref)
        values (v_instance.o_tenant_ref, v_instance.o_ally_ref, 'borrador', 1, p_draft ->> 'clave', v_version,
            p_draft ->> 'etiqueta', (p_draft ->> 'macro_etiqueta')::bigint, p_draft, p_purpose,
            (p_draft ->> 'desde')::timestamptz, (p_draft ->> 'hasta')::timestamptz,
            'automation_expert', p_requested_by_ref)
        returning * into v_campaign;
    else
        select * into v_campaign from public.agent_campaigns c
         where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref
           and c.ally_ref = v_instance.o_ally_ref
         for update;
        if not found then
            raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
        end if;
        if public._agent_campaign_expire_if_due(v_campaign) then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_expired');
        end if;
        if v_campaign.status not in ('borrador', 'validada') then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_draft',
                'status', v_campaign.status);
        end if;
        if v_campaign.revision is distinct from p_expected_revision then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_revision_mismatch',
                'revision', v_campaign.revision);
        end if;
        if v_campaign.revision >= 100 then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_revision_limit');
        end if;
        if v_campaign.campaign_key is distinct from p_draft ->> 'clave' then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_key_is_immutable');
        end if;
        update public.agent_campaigns
           set status = 'borrador', draft = p_draft, purpose = p_purpose, revision = revision + 1,
               label = p_draft ->> 'etiqueta', label_macro_id = (p_draft ->> 'macro_etiqueta')::bigint,
               valid_from = (p_draft ->> 'desde')::timestamptz, valid_until = (p_draft ->> 'hasta')::timestamptz,
               validation = null, fingerprint = null, validated_at = null, expires_at = null,
               check_sha = null, check_report = null, checked_at = null
         where id = v_campaign.id
        returning * into v_campaign;
    end if;

    perform public._agent_campaign_event(v_campaign, 'agent_campaign_draft_saved', 'automation_expert',
        p_requested_by_ref, 'agent_campaign_draft_saved', jsonb_build_object(
            'revision', v_campaign.revision,
            'segment_version', v_campaign.segment_version,
            'draft_sha256', encode(sha256(convert_to(p_draft::text, 'UTF8')), 'hex'),
            'purpose_sha256', encode(sha256(convert_to(p_purpose, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'campaign_id', v_campaign.id, 'revision', v_campaign.revision,
        'segment_version', v_campaign.segment_version, 'status', v_campaign.status);
end;
$function$;

-- Registrar la validacion que corrio el bridge. La base no corre las reglas:
-- le cree a quien registra el resultado, pero arma ella la huella. Sin
-- bloqueos, congela la campana 24 horas.
create function public.record_agent_campaign_validation_v1(
    p_instance jsonb, p_campaign_id uuid, p_revision integer, p_validator jsonb, p_result jsonb,
    p_preview jsonb, p_requested_by_ref text)
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
    v_validations integer;
    v_now timestamptz;
    v_fingerprint text;
    v_validation jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_validator) = 'object'
        and (select array_agg(k order by k) from jsonb_object_keys(p_validator) k)
            = array['contract', 'git_sha', 'product_version', 'rules_version']
        and p_validator ->> 'contract' = 'agent-campaigns-v1'
        and jsonb_typeof(p_validator -> 'rules_version') = 'number'
        and p_validator ->> 'rules_version' ~ '^[1-9][0-9]{0,3}$'
        and p_validator ->> 'product_version' ~ '^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
        and p_validator ->> 'git_sha' ~ '^[0-9a-f]{7,40}$', false) then
        raise exception using errcode = '22023', message = 'agent_campaign_validator_invalid';
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
        raise exception using errcode = '22023', message = 'agent_campaign_result_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_preview) = 'object' and octet_length(p_preview::text) <= 65536, false) then
        raise exception using errcode = '22023', message = 'agent_campaign_preview_invalid';
    end if;
    if p_revision is null then
        raise exception using errcode = '22023', message = 'agent_campaign_revision_invalid';
    end if;

    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    select count(*) into v_validations from public.agent_campaign_events e
     where e.campaign_id = v_campaign.id
       and e.event_type in ('agent_campaign_validated', 'agent_campaign_validation_blocked');
    if v_validations >= 100 then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_validation_limit');
    end if;
    if v_campaign.status is distinct from 'borrador' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_draft', 'status', v_campaign.status);
    end if;
    if v_campaign.revision is distinct from p_revision then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_revision_mismatch',
            'revision', v_campaign.revision);
    end if;

    v_validation := jsonb_build_object('validator', p_validator, 'result', p_result, 'preview', p_preview);
    if jsonb_array_length(p_result -> 'blocks') > 0 then
        update public.agent_campaigns set validation = v_validation where id = v_campaign.id;
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_validation_blocked',
            'automation_expert', p_requested_by_ref, 'agent_campaign_validation_blocked',
            jsonb_build_object('revision', v_campaign.revision,
                'codes', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                            from jsonb_array_elements(p_result -> 'blocks') f),
                'validator', p_validator));
        return jsonb_build_object('ok', true, 'status', 'borrador',
            'blocks', jsonb_array_length(p_result -> 'blocks'));
    end if;

    v_now := clock_timestamp();
    v_fingerprint := public._agent_campaign_fingerprint(v_campaign.id, v_campaign.revision,
        v_campaign.segment_version, v_campaign.draft, v_campaign.purpose);
    update public.agent_campaigns
       set status = 'validada', fingerprint = v_fingerprint, validated_at = v_now,
           expires_at = v_now + interval '24 hours', validation = v_validation
     where id = v_campaign.id
    returning * into v_campaign;
    perform public._agent_campaign_event(v_campaign, 'agent_campaign_validated', 'automation_expert',
        p_requested_by_ref, 'agent_campaign_validated', jsonb_build_object('revision', v_campaign.revision,
            'fingerprint', v_fingerprint, 'validator', p_validator,
            'warnings', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                           from jsonb_array_elements(p_result -> 'warnings') f),
            'expires_at', v_campaign.expires_at));
    return jsonb_build_object('ok', true, 'status', 'validada', 'fingerprint', v_fingerprint,
        'expires_at', v_campaign.expires_at);
end;
$function$;

-- Releer una campana, sin el ID de Slack de nadie.
create function public.get_agent_campaign_v1(p_instance jsonb, p_campaign_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_campaign public.agent_campaigns%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    return public._agent_campaign_view(v_campaign, false);
end;
$function$;

-- Las campanas de la instancia, de la mas nueva a la mas vieja, hasta p_limit
-- (1 a 50). Con p_only_open: los borradores, las validadas sin vencer, y la
-- publicada o pausadas (lo que el AE le cuenta al duenio cuando vuelve).
create function public.list_agent_campaigns_v1(p_instance jsonb, p_only_open boolean, p_limit integer)
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
        raise exception using errcode = '22023', message = 'agent_campaign_list_parameters_invalid';
    end if;
    select coalesce(jsonb_agg(row_data order by created_at desc, campaign_id), '[]'::jsonb)
      into v_rows
      from (
        select c.created_at, c.id as campaign_id, jsonb_build_object(
                   'campaign_id', c.id,
                   'status', c.status,
                   'effective_status', case
                       when c.status = 'validada' and clock_timestamp() >= c.expires_at then 'vencida'
                       when c.status in ('publicada', 'pausada') and clock_timestamp() >= c.valid_until
                           then 'terminada'
                       else c.status end,
                   'revision', c.revision,
                   'campaign_key', c.campaign_key,
                   'segment_version', c.segment_version,
                   'label', c.label,
                   'nombre', c.draft ->> 'nombre',
                   'purpose', c.purpose,
                   'valid_from', c.valid_from,
                   'valid_until', c.valid_until,
                   'expires_at', c.expires_at,
                   'check_clean', c.check_sha is not null,
                   'checked_at', c.checked_at,
                   'approved_at', c.approved_at,
                   'paused_at', c.paused_at,
                   'closed_at', c.closed_at,
                   'created_at', c.created_at) as row_data
          from public.agent_campaigns c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and (not p_only_open
                or c.status in ('borrador', 'publicada', 'pausada')
                or (c.status = 'validada' and clock_timestamp() < c.expires_at))
         order by c.created_at desc, c.id
         limit p_limit) listed;
    return jsonb_build_object('campaigns', v_rows,
        'open', public._agent_campaigns_open(v_instance.o_tenant_ref, v_instance.o_ally_ref),
        'open_limit', 5, 'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 6. Las cinco de la pagina, con la sesion viva.
-- ===========================================================================

create function public.approver_get_agent_campaign_v1(
    p_instance jsonb, p_session_hash text, p_campaign_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_campaign public.agent_campaigns%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    return public._agent_campaign_view(v_campaign, true);
end;
$function$;

-- Publicar: el si del duenio. La huella y el chequeo_sha viajan con el si. El
-- bridge recalcula el chequeo_sha antes de llamar; la base exige que sea el de
-- la fila, que este limpio y que no haya otra viva.
create function public.approver_publish_agent_campaign_v1(
    p_instance jsonb, p_session_hash text, p_campaign_id uuid, p_fingerprint text, p_check_sha text)
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
    v_campaign public.agent_campaigns%rowtype;
    v_other public.agent_campaigns%rowtype;
    v_now timestamptz;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_campaign_fingerprint_invalid';
    end if;
    if not coalesce(p_check_sha ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_campaign_check_sha_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_campaigns:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    if public._agent_campaign_expire_if_due(v_campaign) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_expired');
    end if;
    if v_campaign.fingerprint is not null and p_fingerprint is distinct from v_campaign.fingerprint then
        raise exception using errcode = '22023', message = 'agent_campaign_fingerprint_mismatch';
    end if;
    if v_campaign.status in ('publicada', 'pausada', 'terminada') then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', v_campaign.status);
    end if;
    if v_campaign.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_validated',
            'status', v_campaign.status);
    end if;
    v_now := clock_timestamp();
    if v_campaign.check_sha is null then
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_publish_refused', 'slack', v_actor,
            'agent_campaign_check_missing', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_check_missing');
    end if;
    if p_check_sha is distinct from v_campaign.check_sha then
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_publish_refused', 'slack', v_actor,
            'agent_campaign_check_stale', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_check_stale');
    end if;
    if v_campaign.valid_until <= v_now then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_window_over');
    end if;
    -- Una viva cuya vigencia ya termino no retiene el lugar.
    for v_other in
        select * from public.agent_campaigns c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and c.status = 'publicada' and c.valid_until <= v_now and c.id <> v_campaign.id
         for update
    loop
        update public.agent_campaigns
           set status = 'terminada', closed_at = v_now, closed_by_kind = 'sistema', closed_by_ref = 'vigencia'
         where id = v_other.id;
        perform public._agent_campaign_event(v_other, 'agent_campaign_finished', 'sistema', 'vigencia',
            'agent_campaign_window_over', jsonb_build_object('valid_until', v_other.valid_until));
    end loop;
    if exists (select 1 from public.agent_campaigns c
                where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
                  and c.status = 'publicada' and c.id <> v_campaign.id) then
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_publish_refused', 'slack', v_actor,
            'agent_campaign_live_exists', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_live_exists');
    end if;
    update public.agent_campaigns
       set status = 'publicada', approved_at = v_now, approved_by_kind = 'slack', approved_by_ref = v_actor
     where id = v_campaign.id;
    perform public._agent_campaign_event(v_campaign, 'agent_campaign_published', 'slack', v_actor,
        'agent_campaign_published', jsonb_build_object('fingerprint', v_campaign.fingerprint,
            'check_sha', v_campaign.check_sha, 'segment_version', v_campaign.segment_version));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada');
end;
$function$;

-- Pausar: el agente deja de usarla en la proxima recarga. No borra nada.
create function public.approver_pause_agent_campaign_v1(
    p_instance jsonb, p_session_hash text, p_campaign_id uuid, p_reason text)
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
    v_campaign public.agent_campaigns%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    if v_campaign.status = 'pausada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', 'pausada');
    end if;
    if v_campaign.status is distinct from 'publicada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_live',
            'status', v_campaign.status);
    end if;
    update public.agent_campaigns
       set status = 'pausada', paused_at = clock_timestamp(), paused_by_ref = v_actor, pause_reason = p_reason
     where id = v_campaign.id;
    perform public._agent_campaign_event(v_campaign, 'agent_campaign_paused', 'slack', v_actor,
        'agent_campaign_paused', jsonb_build_object(
            'reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'pausada');
end;
$function$;

-- Reanudar: vuelve la misma campana, con la misma aprobacion. No si ya hay otra viva.
create function public.approver_resume_agent_campaign_v1(
    p_instance jsonb, p_session_hash text, p_campaign_id uuid)
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
    v_campaign public.agent_campaigns%rowtype;
    v_other public.agent_campaigns%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_campaigns:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    if v_campaign.status = 'publicada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', 'publicada');
    end if;
    if v_campaign.status is distinct from 'pausada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_paused',
            'status', v_campaign.status);
    end if;
    if v_campaign.valid_until <= clock_timestamp() then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_window_over');
    end if;
    if exists (select 1 from public.agent_campaigns c
                where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
                  and c.status = 'publicada' and c.id <> v_campaign.id
                  and c.valid_until > clock_timestamp()) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_live_exists');
    end if;
    -- Una viva vencida no retiene el lugar: el indice unico la frenaria igual.
    for v_other in
        select * from public.agent_campaigns c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and c.status = 'publicada' and c.id <> v_campaign.id
         for update
    loop
        update public.agent_campaigns
           set status = 'terminada', closed_at = clock_timestamp(), closed_by_kind = 'sistema', closed_by_ref = 'vigencia'
         where id = v_other.id;
        perform public._agent_campaign_event(v_other, 'agent_campaign_finished', 'sistema', 'vigencia',
            'agent_campaign_window_over', jsonb_build_object('valid_until', v_other.valid_until));
    end loop;
    update public.agent_campaigns
       set status = 'publicada', paused_at = null, paused_by_ref = null, pause_reason = null
     where id = v_campaign.id;
    perform public._agent_campaign_event(v_campaign, 'agent_campaign_resumed', 'slack', v_actor,
        'agent_campaign_resumed', '{}'::jsonb);
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada');
end;
$function$;

-- Descartar, con un motivo de una linea. Solo lo que nunca se publico.
create function public.approver_discard_agent_campaign_v1(
    p_instance jsonb, p_session_hash text, p_campaign_id uuid, p_reason text)
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
    v_campaign public.agent_campaigns%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    if public._agent_campaign_expire_if_due(v_campaign) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_final', 'status', 'vencida');
    end if;
    if v_campaign.status = 'descartada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_campaign.status not in ('borrador', 'validada') then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_final',
            'status', v_campaign.status);
    end if;
    update public.agent_campaigns
       set status = 'descartada', closed_at = clock_timestamp(), closed_by_kind = 'slack',
           closed_by_ref = v_actor, close_reason = p_reason
     where id = v_campaign.id;
    perform public._agent_campaign_event(v_campaign, 'agent_campaign_discarded', 'slack', v_actor,
        'agent_campaign_discarded', jsonb_build_object('from_status', v_campaign.status,
            'close_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

-- ===========================================================================
-- 7. Las de service_role: el catalogo vivo y el comando del juez.
-- ===========================================================================

-- Lo que el bridge sirve: la publicada de la instancia (a lo sumo una). El
-- bridge decide si esta vigente y recalcula el chequeo_sha. Sin IDs de Slack.
create function public.get_live_agent_campaigns_v1(p_instance jsonb)
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
    select coalesce(jsonb_agg(jsonb_build_object(
               'campaign_id', c.id,
               'status', c.status,
               'campaign_key', c.campaign_key,
               'segment_version', c.segment_version,
               'label', c.label,
               'label_macro_id', c.label_macro_id,
               'draft', c.draft,
               'fingerprint', c.fingerprint,
               'check_sha', c.check_sha,
               'approved_at', c.approved_at) order by c.created_at, c.id), '[]'::jsonb)
      into v_rows
      from public.agent_campaigns c
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.status = 'publicada';
    return jsonb_build_object('campaigns', v_rows, 'database_at', clock_timestamp());
end;
$function$;

-- Lo que lee el comando del juez: la fila, para armar el segmento.
create function public.operator_get_agent_campaign_v1(p_instance jsonb, p_campaign_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_campaign public.agent_campaigns%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    return public._agent_campaign_view(v_campaign, false);
end;
$function$;

-- Registrar lo que dio el juez. Atado a la huella: un resultado de otra version
-- del contenido no se puede anotar. Con p_check_sha es limpio (y le da al
-- duenio otras 24 horas para aprobar); sin el, no hay boton. Sobre una publicada
-- o pausada solo vale para volver a certificar (el conocimiento base cambio).
create function public.record_agent_campaign_check_v1(
    p_instance jsonb, p_campaign_id uuid, p_fingerprint text, p_report jsonb, p_check_sha text)
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
    v_now timestamptz;
    v_items jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_campaign_fingerprint_invalid';
    end if;
    if p_check_sha is not null and not coalesce(p_check_sha ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_campaign_check_sha_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_report) = 'object' and octet_length(p_report::text) <= 131072
        and p_report ->> 'veredicto' in ('limpio', 'abiertos', 'sin_veredicto')
        and (p_report ->> 'veredicto' = 'limpio') = (p_check_sha is not null), false) then
        raise exception using errcode = '22023', message = 'agent_campaign_report_invalid';
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
    if v_campaign.status not in ('validada', 'publicada', 'pausada') then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_validated',
            'status', v_campaign.status);
    end if;
    if p_fingerprint is distinct from v_campaign.fingerprint then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_check_of_other_content');
    end if;
    if v_campaign.status <> 'validada' and p_check_sha is null then
        -- Una publicada no se pisa con un resultado sucio: el chequeo_sha de la fila sigue siendo el que vale.
        -- Las preguntas quedan en el historial (hasta las diez primeras, recortadas, por el tope del evento).
        select coalesce(jsonb_agg(jsonb_set(q.item, '{pregunta}',
                   to_jsonb(left(coalesce(q.item ->> 'pregunta', ''), 300)))), '[]'::jsonb)
          into v_items
          from (select value as item from jsonb_array_elements(coalesce(p_report -> 'items', '[]'::jsonb))
                 where jsonb_typeof(value) = 'object' limit 10) q;
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_checked', 'operador', 'comando',
            'agent_campaign_check_not_clean', jsonb_build_object('veredicto', p_report ->> 'veredicto',
                'abiertos', p_report -> 'abiertos', 'items', v_items));
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_check_not_clean',
            'status', v_campaign.status);
    end if;
    v_now := clock_timestamp();
    update public.agent_campaigns
       set check_sha = p_check_sha, check_report = p_report, checked_at = v_now,
           expires_at = case when v_campaign.status = 'validada' and p_check_sha is not null
                             then v_now + interval '24 hours' else expires_at end
     where id = v_campaign.id
    returning * into v_campaign;
    perform public._agent_campaign_event(v_campaign, 'agent_campaign_checked', 'operador', 'comando',
        'agent_campaign_checked', jsonb_build_object('veredicto', p_report ->> 'veredicto',
            'check_sha', p_check_sha));
    return jsonb_build_object('ok', true, 'status', v_campaign.status, 'clean', p_check_sha is not null,
        'expires_at', v_campaign.expires_at);
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
    foreach v_table in array array['agent_campaigns', 'agent_campaign_events'] loop
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
        'public._agent_campaign_draft_is_valid(jsonb)',
        'public.reject_agent_campaign_mutation()',
        'public.validate_agent_campaign_change()',
        'public._agent_campaign_event(public.agent_campaigns,text,text,text,text,jsonb)',
        'public._agent_campaign_fingerprint(uuid,integer,integer,jsonb,text)',
        'public._agent_campaign_expire_if_due(public.agent_campaigns)',
        'public._agent_campaign_view(public.agent_campaigns,boolean)',
        'public._agent_campaigns_open(text,text)',
        'public.save_agent_campaign_draft_v1(jsonb,uuid,integer,jsonb,text,text)',
        'public.record_agent_campaign_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,text)',
        'public.get_agent_campaign_v1(jsonb,uuid)',
        'public.list_agent_campaigns_v1(jsonb,boolean,integer)',
        'public.approver_get_agent_campaign_v1(jsonb,text,uuid)',
        'public.approver_publish_agent_campaign_v1(jsonb,text,uuid,text,text)',
        'public.approver_pause_agent_campaign_v1(jsonb,text,uuid,text)',
        'public.approver_resume_agent_campaign_v1(jsonb,text,uuid)',
        'public.approver_discard_agent_campaign_v1(jsonb,text,uuid,text)',
        'public.get_live_agent_campaigns_v1(jsonb)',
        'public.operator_get_agent_campaign_v1(jsonb,uuid)',
        'public.record_agent_campaign_check_v1(jsonb,uuid,text,jsonb,text)'
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
            'public.save_agent_campaign_draft_v1(jsonb,uuid,integer,jsonb,text,text)',
            'public.record_agent_campaign_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,text)',
            'public.get_agent_campaign_v1(jsonb,uuid)',
            'public.list_agent_campaigns_v1(jsonb,boolean,integer)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to automation_expert_change', v_function);
        end loop;
    end if;
    if to_regrole('flow_change_approver') is not null then
        foreach v_function in array array[
            'public.approver_get_agent_campaign_v1(jsonb,text,uuid)',
            'public.approver_publish_agent_campaign_v1(jsonb,text,uuid,text,text)',
            'public.approver_pause_agent_campaign_v1(jsonb,text,uuid,text)',
            'public.approver_resume_agent_campaign_v1(jsonb,text,uuid)',
            'public.approver_discard_agent_campaign_v1(jsonb,text,uuid,text)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end loop;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.get_live_agent_campaigns_v1(jsonb) to service_role;
        grant execute on function public.operator_get_agent_campaign_v1(jsonb, uuid) to service_role;
        grant execute on function public.record_agent_campaign_check_v1(jsonb, uuid, text, jsonb, text) to service_role;
    end if;
end;
$acl$;

commit;
