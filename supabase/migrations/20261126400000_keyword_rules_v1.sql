-- keyword-rules-v1: la regla «palabra clave -> etiqueta» que redacta el
-- Automation Expert y aprueba una persona de la lista en la pagina (Z1 3.9,
-- D4 del mapa del 2026-11-26). Contrato: docs/contracts/keyword-rules-v1.md.
--
-- El link wa.me de un segmento (soporte, check-in, asistencia, alumnos) lleva un
-- texto prellenado. Cuando el PRIMER mensaje de una conversacion trae la palabra
-- clave de una regla publicada, el bridge le pone la etiqueta de la regla por su
-- macro (que solo suma esa etiqueta) y le avisa al agente, en ese turno, que el
-- mensaje es el texto de un link y no un pedido de hablar con una persona.
--
-- 1. Dos tablas nuevas, vacias: keyword_rules (una fila por regla) y
--    keyword_rule_events (solo agregar). Sin permisos para nadie de la API, con
--    RLS.
-- 2. Tres funciones del Automation Expert, solo para automation_expert_change
--    (20261021000100): guardar o rehacer un borrador, releer una regla y
--    listarlas. Ni publicar, ni pausar, ni descartar.
-- 3. Cinco funciones de la pagina, solo para flow_change_approver
--    (20261028000100), todas con la sesion viva: el actor slack sale de la fila
--    de la sesion, nunca de un parametro. Releer, publicar, pausar, reanudar y
--    descartar.
-- 4. Una para service_role: la lectura de las reglas vivas que hace el bridge.
--
-- La misma palabra clave (normalizada) no puede estar en dos reglas vivas o
-- pausadas de la instancia: un indice unico parcial lo hace cumplir. Que una
-- palabra no contenga a otra, ni choque con la de una campana, lo mira el bridge
-- antes de publicar (la normalizacion es suya).
--
-- La regla v1 no dispara flujos: no hay clave para eso en la forma, y la base
-- rechaza una que la traiga. Ver el contrato, «El flujo».
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- KEYWORD_RULES_ENABLED el bridge no llama a ninguna de estas funciones.
-- Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 28000 sesion invalida
-- (flow_approval_session_invalid); P0002 no existe o no es de esta instancia;
-- 55000 un estado que no lo permite. Los resultados del ciclo (revision vieja,
-- tope, palabra tomada...) vuelven como {"ok": false, "reason_code"}.

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
        raise exception using errcode = '55000', message = 'keyword_rules_prerequisite_missing';
    end if;
    if to_regclass('public.keyword_rules') is not null then
        raise exception using errcode = '55000', message = 'keyword_rules_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. La forma que usan los CHECK.
-- ===========================================================================

-- La regla normalizada que guarda el bridge (keyword_rules.py). La base no
-- normaliza: mira la forma, para que una fila no pueda traer otra cosa.
create function public._keyword_rule_is_valid(p_rule jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
declare
    v_keys text[];
begin
    if jsonb_typeof(p_rule) is distinct from 'object' or octet_length(p_rule::text) > 4096 then
        return false;
    end if;
    select array_agg(k order by k) into v_keys from jsonb_object_keys(p_rule) k;
    if v_keys is distinct from array['clave', 'etiqueta', 'macro_etiqueta', 'nombre', 'palabra_clave',
                                     'palabra_normalizada'] then
        return false;
    end if;
    return coalesce(
        jsonb_typeof(p_rule -> 'clave') = 'string'
        and p_rule ->> 'clave' ~ '^[a-z0-9][a-z0-9-]{0,63}$'
        and jsonb_typeof(p_rule -> 'nombre') = 'string'
        and length(p_rule ->> 'nombre') between 1 and 80
        and jsonb_typeof(p_rule -> 'palabra_clave') = 'string'
        and length(p_rule ->> 'palabra_clave') between 1 and 200
        and jsonb_typeof(p_rule -> 'palabra_normalizada') = 'string'
        and length(p_rule ->> 'palabra_normalizada') between 3 and 120
        and p_rule ->> 'palabra_normalizada' ~ '^[[:alnum:]]+( [[:alnum:]]+)*$'
        and jsonb_typeof(p_rule -> 'etiqueta') = 'string'
        and p_rule ->> 'etiqueta' ~ '^[a-z0-9][a-z0-9_-]{0,39}$'
        and jsonb_typeof(p_rule -> 'macro_etiqueta') = 'number'
        and (p_rule ->> 'macro_etiqueta') ~ '^[1-9][0-9]{0,11}$', false);
end;
$function$;

-- ===========================================================================
-- 2. Las tablas.
-- ===========================================================================

create table public.keyword_rules (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    status text not null check (status in ('borrador', 'publicada', 'pausada', 'descartada')),
    revision integer not null check (revision > 0),
    rule_key text not null check (rule_key ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    keyword text not null,
    label text not null check (label ~ '^[a-z0-9][a-z0-9_-]{0,39}$'),
    label_macro_id bigint not null check (label_macro_id > 0),
    rule jsonb not null check (public._keyword_rule_is_valid(rule)),
    purpose text not null check (public._flow_store_free_text_is_valid(purpose)),
    fingerprint text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
    requested_by_kind text not null check (requested_by_kind = 'automation_expert'),
    requested_by_ref text not null,
    approved_at timestamptz,
    approved_by_kind text check (approved_by_kind = 'slack'),
    approved_by_ref text,
    paused_at timestamptz,
    paused_by_ref text,
    pause_reason text check (pause_reason is null or public._flow_store_free_text_is_valid(pause_reason)),
    closed_at timestamptz,
    closed_by_kind text check (closed_by_kind = 'slack'),
    closed_by_ref text,
    close_reason text check (close_reason is null or public._flow_store_free_text_is_valid(close_reason)),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    constraint keyword_rules_requested_by
        check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    constraint keyword_rules_approved_by
        check ((approved_by_kind is null and approved_by_ref is null)
               or public._flow_store_actor_is_valid(approved_by_kind, approved_by_ref)),
    constraint keyword_rules_paused_by
        check (paused_by_ref is null or public._flow_store_actor_is_valid('slack', paused_by_ref)),
    constraint keyword_rules_closed_by
        check ((closed_by_kind is null and closed_by_ref is null)
               or public._flow_store_actor_is_valid(closed_by_kind, closed_by_ref)),
    constraint keyword_rules_identity_matches_rule
        check (rule ->> 'clave' = rule_key and rule ->> 'palabra_normalizada' = keyword
               and rule ->> 'etiqueta' = label and (rule ->> 'macro_etiqueta')::bigint = label_macro_id),
    -- El CASE entero adentro de coalesce: una comparacion con NULL no deja pasar la fila.
    constraint keyword_rules_state_coherent check (coalesce(case status
        when 'borrador' then approved_at is null and approved_by_kind is null and paused_at is null
            and closed_at is null
        when 'publicada' then approved_at is not null and approved_by_kind is not null and paused_at is null
            and closed_at is null
        when 'pausada' then approved_at is not null and approved_by_kind is not null and paused_at is not null
            and paused_by_ref is not null and closed_at is null
        when 'descartada' then closed_at is not null and closed_by_kind is not null and close_reason is not null
    end, false))
);

-- El id es un uuid y no un identity: una secuencia recibiria permisos por los
-- privilegios por defecto de Supabase.
create table public.keyword_rule_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    rule_id uuid not null references public.keyword_rules (id) on delete restrict,
    event_type text not null check (event_type in (
        'keyword_rule_draft_saved', 'keyword_rule_published', 'keyword_rule_publish_refused',
        'keyword_rule_paused', 'keyword_rule_resumed', 'keyword_rule_discarded')),
    actor_kind text not null check (actor_kind in ('automation_expert', 'slack')),
    actor_ref text not null,
    reason_code text not null check (reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'),
    data jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object'
        and octet_length(data::text) <= 16384),
    created_at timestamptz not null default clock_timestamp(),
    constraint keyword_rule_events_actor
        check (public._flow_store_actor_is_valid(actor_kind, actor_ref))
);

-- **La misma palabra no esta en dos reglas que pueden volver a servirse.**
create unique index keyword_rules_one_keyword
    on public.keyword_rules (tenant_ref, ally_ref, keyword) where status in ('publicada', 'pausada');
-- Una clave nombra una sola regla abierta o viva.
create unique index keyword_rules_one_key
    on public.keyword_rules (tenant_ref, ally_ref, rule_key) where status <> 'descartada';
create index keyword_rules_instance_time_idx
    on public.keyword_rules (tenant_ref, ally_ref, created_at desc);
create index keyword_rule_events_rule_time_idx
    on public.keyword_rule_events (rule_id, created_at);

alter table public.keyword_rules enable row level security;
alter table public.keyword_rule_events enable row level security;

-- ===========================================================================
-- 3. Los disparadores.
-- ===========================================================================

create function public.reject_keyword_rule_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'keyword_rule_events' then 'keyword_rule_event_is_append_only'
        else 'keyword_rule_is_permanent'
    end;
end;
$function$;

-- El recorrido. Solo el borrador cambia de contenido (rehacerlo sube la
-- revision y la huella); desde publicada quedan congelados el contenido y la
-- aprobacion; descartada no acepta ningun UPDATE.
create function public.validate_keyword_rule_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if old.status = 'descartada' then
        raise exception using errcode = '55000', message = 'keyword_rule_frozen';
    end if;
    if row(new.id, new.tenant_ref, new.ally_ref, new.rule_key, new.requested_by_kind, new.requested_by_ref,
           new.created_at)
       is distinct from row(old.id, old.tenant_ref, old.ally_ref, old.rule_key, old.requested_by_kind,
                            old.requested_by_ref, old.created_at) then
        raise exception using errcode = '55000', message = 'keyword_rule_identity_is_immutable';
    end if;
    if new.status is distinct from old.status and not (
        (old.status = 'borrador' and new.status in ('publicada', 'descartada'))
        or (old.status = 'publicada' and new.status = 'pausada')
        or (old.status = 'pausada' and new.status in ('publicada', 'descartada'))
    ) then
        raise exception using errcode = '23514', message = 'keyword_rule_transition_invalid';
    end if;
    if old.status = 'borrador' and new.status = 'borrador' then
        if row(new.rule, new.purpose) is distinct from row(old.rule, old.purpose)
           and new.revision is distinct from old.revision + 1 then
            raise exception using errcode = '23514', message = 'keyword_rule_revision_must_increment';
        end if;
    elsif row(new.rule, new.purpose, new.revision, new.keyword, new.label, new.label_macro_id, new.fingerprint)
          is distinct from row(old.rule, old.purpose, old.revision, old.keyword, old.label, old.label_macro_id,
                               old.fingerprint) then
        raise exception using errcode = '55000', message = 'keyword_rule_frozen';
    end if;
    if old.status in ('publicada', 'pausada')
       and row(new.approved_at, new.approved_by_kind, new.approved_by_ref)
           is distinct from row(old.approved_at, old.approved_by_kind, old.approved_by_ref) then
        raise exception using errcode = '55000', message = 'keyword_rule_frozen';
    end if;
    return new;
end;
$function$;

create trigger keyword_rules_validate
before update on public.keyword_rules
for each row execute function public.validate_keyword_rule_change();
create trigger keyword_rules_permanent
before delete on public.keyword_rules
for each row execute function public.reject_keyword_rule_mutation();
create trigger keyword_rules_no_truncate
before truncate on public.keyword_rules
for each statement execute function public.reject_keyword_rule_mutation();
create trigger keyword_rules_set_updated_at
before update on public.keyword_rules
for each row execute function public.set_updated_at();
create trigger keyword_rule_events_append_only
before update or delete on public.keyword_rule_events
for each row execute function public.reject_keyword_rule_mutation();
create trigger keyword_rule_events_no_truncate
before truncate on public.keyword_rule_events
for each statement execute function public.reject_keyword_rule_mutation();

-- ===========================================================================
-- 4. Los helpers internos (security invoker, de nadie de la API).
-- ===========================================================================

create function public._keyword_rule_event(
    p_rule public.keyword_rules, p_event_type text, p_actor_kind text, p_actor_ref text,
    p_reason_code text, p_data jsonb)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.keyword_rule_events (tenant_ref, ally_ref, rule_id, event_type,
        actor_kind, actor_ref, reason_code, data)
    values (p_rule.tenant_ref, p_rule.ally_ref, p_rule.id, p_event_type,
        p_actor_kind, p_actor_ref, p_reason_code, coalesce(p_data, '{}'::jsonb))
$function$;

-- La huella la calcula solo la base, sobre el texto del jsonb, al guardar.
create function public._keyword_rule_fingerprint(
    p_rule_id uuid, p_revision integer, p_rule jsonb, p_purpose text)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select encode(sha256(convert_to(jsonb_build_object(
        'contract', 'keyword-rules-v1', 'rule_id', p_rule_id, 'revision', p_revision,
        'rule', p_rule, 'purpose', p_purpose)::text, 'UTF8')), 'hex')
$function$;

-- Lo que se muestra de una regla. Con p_with_people (la pagina), el ID de Slack
-- de quien la aprobo y de quien la pauso; el agente no lo recibe. Nunca quien la pidio.
create function public._keyword_rule_view(p_rule public.keyword_rules, p_with_people boolean)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'rule_id', p_rule.id,
        'status', p_rule.status,
        'revision', p_rule.revision,
        'rule_key', p_rule.rule_key,
        'keyword', p_rule.keyword,
        'label', p_rule.label,
        'rule', p_rule.rule,
        'purpose', p_rule.purpose,
        'fingerprint', p_rule.fingerprint,
        'approved_at', p_rule.approved_at,
        'paused_at', p_rule.paused_at,
        'closed_at', p_rule.closed_at,
        'created_at', p_rule.created_at,
        'events', coalesce((
            select jsonb_agg(jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                'reason_code', e.reason_code, 'created_at', e.created_at)
                             order by e.created_at desc, e.id)
              from (select * from public.keyword_rule_events e
                     where e.rule_id = p_rule.id
                     order by e.created_at desc, e.id limit 50) e), '[]'::jsonb))
        || case when p_with_people then jsonb_build_object('approved_by_ref', p_rule.approved_by_ref,
                                                           'paused_by_ref', p_rule.paused_by_ref)
                else '{}'::jsonb end
$function$;

-- ===========================================================================
-- 5. Las tres del Automation Expert.
-- ===========================================================================

-- Guardar o rehacer un borrador. Hasta 10 borradores abiertos por instancia. La
-- huella cambia con cada revision: una pagina abierta antes ya no puede publicarla.
create function public.save_keyword_rule_draft_v1(
    p_instance jsonb, p_rule_id uuid, p_expected_revision integer, p_rule jsonb, p_purpose text,
    p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_rule public.keyword_rules%rowtype;
    v_open integer;
    v_id uuid;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_purpose) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    if not coalesce(public._keyword_rule_is_valid(p_rule), false) then
        raise exception using errcode = '22023', message = 'keyword_rule_invalid';
    end if;
    if (p_rule_id is null) is distinct from (p_expected_revision is null) then
        raise exception using errcode = '22023', message = 'keyword_rule_redo_invalid';
    end if;

    -- Un lock por instancia: dos borradores a la vez no pasan el tope ni toman la misma clave.
    perform set_config('lock_timeout', '5s', true);
    perform pg_advisory_xact_lock(hashtextextended(
        'keyword_rules:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));

    if p_rule_id is null then
        select count(*)::integer into v_open from public.keyword_rules r
         where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
           and r.status = 'borrador';
        if v_open >= 10 then
            return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_open_limit', 'open', v_open);
        end if;
        if exists (select 1 from public.keyword_rules r
                    where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
                      and r.rule_key = p_rule ->> 'clave' and r.status <> 'descartada') then
            return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_key_taken');
        end if;
        v_id := gen_random_uuid();
        insert into public.keyword_rules (id, tenant_ref, ally_ref, status, revision, rule_key, keyword, label,
            label_macro_id, rule, purpose, fingerprint, requested_by_kind, requested_by_ref)
        values (v_id, v_instance.o_tenant_ref, v_instance.o_ally_ref, 'borrador', 1, p_rule ->> 'clave',
            p_rule ->> 'palabra_normalizada', p_rule ->> 'etiqueta', (p_rule ->> 'macro_etiqueta')::bigint,
            p_rule, p_purpose, public._keyword_rule_fingerprint(v_id, 1, p_rule, p_purpose),
            'automation_expert', p_requested_by_ref)
        returning * into v_rule;
    else
        select * into v_rule from public.keyword_rules r
         where r.id = p_rule_id and r.tenant_ref = v_instance.o_tenant_ref
           and r.ally_ref = v_instance.o_ally_ref
         for update;
        if not found then
            raise exception using errcode = 'P0002', message = 'keyword_rule_not_found';
        end if;
        if v_rule.status is distinct from 'borrador' then
            return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_not_draft',
                'status', v_rule.status);
        end if;
        if v_rule.revision is distinct from p_expected_revision then
            return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_revision_mismatch',
                'revision', v_rule.revision);
        end if;
        if v_rule.revision >= 100 then
            return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_revision_limit');
        end if;
        if v_rule.rule_key is distinct from p_rule ->> 'clave' then
            return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_key_is_immutable');
        end if;
        update public.keyword_rules
           set rule = p_rule, purpose = p_purpose, revision = revision + 1,
               keyword = p_rule ->> 'palabra_normalizada', label = p_rule ->> 'etiqueta',
               label_macro_id = (p_rule ->> 'macro_etiqueta')::bigint,
               fingerprint = public._keyword_rule_fingerprint(v_rule.id, v_rule.revision + 1, p_rule, p_purpose)
         where id = v_rule.id
        returning * into v_rule;
    end if;

    perform public._keyword_rule_event(v_rule, 'keyword_rule_draft_saved', 'automation_expert',
        p_requested_by_ref, 'keyword_rule_draft_saved', jsonb_build_object(
            'revision', v_rule.revision,
            'rule_sha256', encode(sha256(convert_to(p_rule::text, 'UTF8')), 'hex'),
            'purpose_sha256', encode(sha256(convert_to(p_purpose, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'rule_id', v_rule.id, 'revision', v_rule.revision,
        'status', v_rule.status, 'fingerprint', v_rule.fingerprint);
end;
$function$;

-- Releer una regla, sin el ID de Slack de nadie.
create function public.get_keyword_rule_v1(p_instance jsonb, p_rule_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_rule public.keyword_rules%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_rule from public.keyword_rules r
     where r.id = p_rule_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'keyword_rule_not_found';
    end if;
    return public._keyword_rule_view(v_rule, false);
end;
$function$;

-- Las reglas de la instancia, de la mas nueva a la mas vieja, hasta p_limit (1
-- a 50). Con p_only_open: borradores, publicadas y pausadas.
create function public.list_keyword_rules_v1(p_instance jsonb, p_only_open boolean, p_limit integer)
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
        raise exception using errcode = '22023', message = 'keyword_rule_list_parameters_invalid';
    end if;
    select coalesce(jsonb_agg(row_data order by created_at desc, rule_id), '[]'::jsonb)
      into v_rows
      from (
        select r.created_at, r.id as rule_id, jsonb_build_object(
                   'rule_id', r.id,
                   'status', r.status,
                   'revision', r.revision,
                   'rule_key', r.rule_key,
                   'nombre', r.rule ->> 'nombre',
                   'palabra_clave', r.rule ->> 'palabra_clave',
                   'keyword', r.keyword,
                   'label', r.label,
                   'purpose', r.purpose,
                   'approved_at', r.approved_at,
                   'paused_at', r.paused_at,
                   'closed_at', r.closed_at,
                   'created_at', r.created_at) as row_data
          from public.keyword_rules r
         where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
           and (not p_only_open or r.status <> 'descartada')
         order by r.created_at desc, r.id
         limit p_limit) listed;
    return jsonb_build_object('rules', v_rows, 'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 6. Las cinco de la pagina, con la sesion viva.
-- ===========================================================================

create function public.approver_get_keyword_rule_v1(
    p_instance jsonb, p_session_hash text, p_rule_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_rule public.keyword_rules%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_rule from public.keyword_rules r
     where r.id = p_rule_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'keyword_rule_not_found';
    end if;
    return public._keyword_rule_view(v_rule, true);
end;
$function$;

-- Publicar: el si de una persona de la lista. La huella viaja con el si. El
-- bridge revisa antes que la palabra no choque con otra regla ni con una
-- campana y que el macro solo sume la etiqueta; la base exige la misma palabra
-- en una sola regla y un tope de 20 vivas.
create function public.approver_publish_keyword_rule_v1(
    p_instance jsonb, p_session_hash text, p_rule_id uuid, p_fingerprint text)
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
    v_rule public.keyword_rules%rowtype;
    v_live integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'keyword_rule_fingerprint_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'keyword_rules:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_rule from public.keyword_rules r
     where r.id = p_rule_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'keyword_rule_not_found';
    end if;
    if p_fingerprint is distinct from v_rule.fingerprint then
        raise exception using errcode = '22023', message = 'keyword_rule_fingerprint_mismatch';
    end if;
    if v_rule.status in ('publicada', 'pausada') then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', v_rule.status);
    end if;
    if v_rule.status is distinct from 'borrador' then
        return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_final', 'status', v_rule.status);
    end if;
    if exists (select 1 from public.keyword_rules r
                where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
                  and r.keyword = v_rule.keyword and r.status in ('publicada', 'pausada')
                  and r.id <> v_rule.id) then
        perform public._keyword_rule_event(v_rule, 'keyword_rule_publish_refused', 'slack', v_actor,
            'keyword_rule_word_taken', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_word_taken');
    end if;
    select count(*)::integer into v_live from public.keyword_rules r
     where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and r.status = 'publicada';
    if v_live >= 20 then
        perform public._keyword_rule_event(v_rule, 'keyword_rule_publish_refused', 'slack', v_actor,
            'keyword_rule_live_limit', jsonb_build_object('live', v_live));
        return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_live_limit');
    end if;
    update public.keyword_rules
       set status = 'publicada', approved_at = clock_timestamp(), approved_by_kind = 'slack',
           approved_by_ref = v_actor
     where id = v_rule.id;
    perform public._keyword_rule_event(v_rule, 'keyword_rule_published', 'slack', v_actor,
        'keyword_rule_published', jsonb_build_object('fingerprint', v_rule.fingerprint,
            'revision', v_rule.revision));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada');
end;
$function$;

-- Pausar: el bridge deja de usarla en la proxima recarga. No borra nada.
create function public.approver_pause_keyword_rule_v1(
    p_instance jsonb, p_session_hash text, p_rule_id uuid, p_reason text)
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
    v_rule public.keyword_rules%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_rule from public.keyword_rules r
     where r.id = p_rule_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'keyword_rule_not_found';
    end if;
    if v_rule.status = 'pausada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_rule.status is distinct from 'publicada' then
        return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_not_live', 'status', v_rule.status);
    end if;
    update public.keyword_rules
       set status = 'pausada', paused_at = clock_timestamp(), paused_by_ref = v_actor, pause_reason = p_reason
     where id = v_rule.id;
    perform public._keyword_rule_event(v_rule, 'keyword_rule_paused', 'slack', v_actor, 'keyword_rule_paused',
        jsonb_build_object('pause_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

-- Reanudar una pausada. El bridge vuelve a revisar los choques antes de llamar.
create function public.approver_resume_keyword_rule_v1(
    p_instance jsonb, p_session_hash text, p_rule_id uuid)
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
    v_rule public.keyword_rules%rowtype;
    v_live integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    perform pg_advisory_xact_lock(hashtextextended(
        'keyword_rules:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_rule from public.keyword_rules r
     where r.id = p_rule_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'keyword_rule_not_found';
    end if;
    if v_rule.status = 'publicada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_rule.status is distinct from 'pausada' then
        return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_not_paused', 'status', v_rule.status);
    end if;
    select count(*)::integer into v_live from public.keyword_rules r
     where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and r.status = 'publicada';
    if v_live >= 20 then
        return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_live_limit');
    end if;
    update public.keyword_rules
       set status = 'publicada', paused_at = null, paused_by_ref = null, pause_reason = null
     where id = v_rule.id;
    perform public._keyword_rule_event(v_rule, 'keyword_rule_resumed', 'slack', v_actor, 'keyword_rule_resumed',
        '{}'::jsonb);
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

-- Descartar un borrador o una pausada. Lo descartado no vuelve.
create function public.approver_discard_keyword_rule_v1(
    p_instance jsonb, p_session_hash text, p_rule_id uuid, p_reason text)
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
    v_rule public.keyword_rules%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_rule from public.keyword_rules r
     where r.id = p_rule_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'keyword_rule_not_found';
    end if;
    if v_rule.status = 'descartada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_rule.status not in ('borrador', 'pausada') then
        return jsonb_build_object('ok', false, 'reason_code', 'keyword_rule_live_pause_first',
            'status', v_rule.status);
    end if;
    update public.keyword_rules
       set status = 'descartada', closed_at = clock_timestamp(), closed_by_kind = 'slack',
           closed_by_ref = v_actor, close_reason = p_reason
     where id = v_rule.id;
    perform public._keyword_rule_event(v_rule, 'keyword_rule_discarded', 'slack', v_actor,
        'keyword_rule_discarded', jsonb_build_object('from_status', v_rule.status,
            'close_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

-- ===========================================================================
-- 7. La de service_role: lo que sirve el bridge.
-- ===========================================================================

-- Las publicadas de la instancia, sin IDs de Slack. El bridge decide si se
-- sirven (que no choquen con una campana y que el macro sirva).
create function public.get_live_keyword_rules_v1(p_instance jsonb)
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
               'rule_id', r.id,
               'rule_key', r.rule_key,
               'revision', r.revision,
               'rule', r.rule,
               'fingerprint', r.fingerprint,
               'approved_at', r.approved_at) order by r.approved_at, r.id), '[]'::jsonb)
      into v_rows
      from public.keyword_rules r
     where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and r.status = 'publicada';
    return jsonb_build_object('rules', v_rows, 'database_at', clock_timestamp());
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
    foreach v_table in array array['keyword_rules', 'keyword_rule_events'] loop
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
        'public._keyword_rule_is_valid(jsonb)',
        'public.reject_keyword_rule_mutation()',
        'public.validate_keyword_rule_change()',
        'public._keyword_rule_event(public.keyword_rules,text,text,text,text,jsonb)',
        'public._keyword_rule_fingerprint(uuid,integer,jsonb,text)',
        'public._keyword_rule_view(public.keyword_rules,boolean)',
        'public.save_keyword_rule_draft_v1(jsonb,uuid,integer,jsonb,text,text)',
        'public.get_keyword_rule_v1(jsonb,uuid)',
        'public.list_keyword_rules_v1(jsonb,boolean,integer)',
        'public.approver_get_keyword_rule_v1(jsonb,text,uuid)',
        'public.approver_publish_keyword_rule_v1(jsonb,text,uuid,text)',
        'public.approver_pause_keyword_rule_v1(jsonb,text,uuid,text)',
        'public.approver_resume_keyword_rule_v1(jsonb,text,uuid)',
        'public.approver_discard_keyword_rule_v1(jsonb,text,uuid,text)',
        'public.get_live_keyword_rules_v1(jsonb)'
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
            'public.save_keyword_rule_draft_v1(jsonb,uuid,integer,jsonb,text,text)',
            'public.get_keyword_rule_v1(jsonb,uuid)',
            'public.list_keyword_rules_v1(jsonb,boolean,integer)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to automation_expert_change', v_function);
        end loop;
    end if;
    if to_regrole('flow_change_approver') is not null then
        foreach v_function in array array[
            'public.approver_get_keyword_rule_v1(jsonb,text,uuid)',
            'public.approver_publish_keyword_rule_v1(jsonb,text,uuid,text)',
            'public.approver_pause_keyword_rule_v1(jsonb,text,uuid,text)',
            'public.approver_resume_keyword_rule_v1(jsonb,text,uuid)',
            'public.approver_discard_keyword_rule_v1(jsonb,text,uuid,text)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end loop;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.get_live_keyword_rules_v1(jsonb) to service_role;
    end if;
end;
$roles$;

commit;
