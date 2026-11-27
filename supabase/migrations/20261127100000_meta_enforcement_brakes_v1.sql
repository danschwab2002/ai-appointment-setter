-- meta-enforcement-brakes-v1: los frenos ante Meta (F1 del mapa del 2026-11-27,
-- «Frenos ante Meta y calentamiento», decisiones D2 a D6). Contrato:
-- docs/contracts/meta-enforcement-brakes-v1.md.
--
-- Tres cosas que Meta le hace a una cuenta de WhatsApp y que hoy el bridge no ve:
--
-- * Recategoriza una plantilla aprobada de utilidad a marketing (o a
--   autenticacion). Sigue APPROVED y se sigue mandando, a precio de marketing,
--   y cada recategorizacion acerca la restriccion de utilidad del portfolio.
-- * Avisa antes (correct_category, 24 h) cuando el negocio no esta advertido.
-- * Restringe la cuenta (account_update, ACCOUNT_RESTRICTION con
--   violation_info.violation_type UTILITY_TEMPLATE_ABUSE*).
--
-- Dos entradas escriben el mismo registro: el sondeo del bridge (cada 15 min
-- lee category y correct_category de todas las plantillas de la cuenta) y la
-- ruta /webhooks/meta (template_category_update y account_update). Lo que
-- decide (retirar, contar, congelar, avisar) se decide aca, una sola vez.
--
-- 1. Siete tablas nuevas, vacias, con RLS y sin permisos para la API:
--    lo ultimo visto de cada plantilla, los eventos de categoria, las
--    plantillas retiradas, los eventos de la cuenta, los congelamientos, las
--    liberaciones del contador y los avisos.
-- 2. Siete funciones de service_role: registrar el sondeo, registrar un
--    webhook de categoria, registrar un evento de la cuenta, leer el estado,
--    la accion a mano (congelar, descongelar, liberar una plantilla, liberar
--    el contador), y reclamar y cerrar los avisos.
--
-- D-F1.1 La primera lectura del sondeo es linea de base: no recategoriza para
--        atras. Una plantilla que ya era de marketing la primera vez que se ve
--        no se retira.
-- D-F1.2 Solo retira el paso de UTILITY a MARKETING o AUTHENTICATION ya hecho.
--        El aviso previo solo avisa. De marketing a utilidad no hace nada.
-- D-F1.3 El contador (D4) cuenta los eventos «hecho» de la cuenta (sondeo,
--        webhook y el de aprobar: una solicitud pedida como UTILITY que Meta
--        aprobo como otra categoria) posteriores al ultimo reinicio, que es la
--        ultima solicitud pedida como UTILITY aprobada como UTILITY o la
--        ultima liberacion a mano. La aprobacion como otra categoria cuenta
--        pero no retira: esa plantilla nunca fue de utilidad.
-- D-F1.4 Un mismo cambio no se registra dos veces: un «hecho» igual (plantilla,
--        idioma, categoria destino) en los ultimos 7 dias, o con la plantilla
--        todavia retirada, es repetido; un aviso previo igual en 7 dias, tambien.
-- D-F1.5 Restriccion: congela todo ACCOUNT_RESTRICTION salvo
--        UTILITY_TEMPLATE_ABUSE_UNBAN y UTILITY_TEMPLATE_ABUSE_RATE_LIMIT_RECOVERY,
--        que solo avisan. Tambien la advertencia (UTILITY_TEMPLATE_ABUSE sin
--        restriction_info), del lado prudente: la levanta una persona. Nada
--        descongela solo.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- META_ENFORCEMENT_BRAKES_ENABLED el bridge no llama a ninguna de estas
-- funciones. Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_store_actor_is_valid(text,text)') is null
       or to_regprocedure('public._flow_store_free_text_is_valid(text)') is null
       or to_regclass('public.meta_template_requests') is null then
        raise exception using errcode = '55000', message = 'meta_enforcement_prerequisite_missing';
    end if;
    if to_regclass('public.meta_template_category_events') is not null then
        raise exception using errcode = '55000', message = 'meta_enforcement_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. Las tablas.
-- ===========================================================================

-- Lo ultimo que el sondeo (o un webhook hecho) vio de cada plantilla.
create table public.meta_template_category_seen (
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    waba_id text not null check (waba_id ~ '^[0-9]{6,32}$'),
    template_name text not null check (template_name ~ '^[a-z0-9][a-z0-9_]{0,249}$'),
    language text not null check (language ~ '^[a-z]{2,3}(_[A-Z]{2})?$'),
    meta_template_id text check (meta_template_id ~ '^[0-9]{1,32}$'),
    category text not null check (category in ('UTILITY', 'MARKETING', 'AUTHENTICATION')),
    correct_category text check (correct_category in ('UTILITY', 'MARKETING', 'AUTHENTICATION')),
    seen_at timestamptz not null,
    primary key (tenant_ref, ally_ref, waba_id, template_name, language)
);

create table public.meta_template_category_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    waba_id text not null check (waba_id ~ '^[0-9]{6,32}$'),
    template_name text not null check (template_name ~ '^[a-z0-9][a-z0-9_]{0,249}$'),
    language text not null check (language ~ '^[a-z]{2,3}(_[A-Z]{2})?$'),
    meta_template_id text check (meta_template_id ~ '^[0-9]{1,32}$'),
    -- aviso_previo: Meta anuncia el cambio (correct_category); hecho: ya cambio.
    kind text not null check (kind in ('aviso_previo', 'hecho')),
    source text not null check (source in ('sondeo', 'webhook', 'aprobacion')),
    previous_category text check (previous_category in ('UTILITY', 'MARKETING', 'AUTHENTICATION')),
    new_category text not null check (new_category in ('UTILITY', 'MARKETING', 'AUTHENTICATION')),
    correct_category text check (correct_category in ('UTILITY', 'MARKETING', 'AUTHENTICATION')),
    -- Cuando Meta dice que cambia (aviso previo, category_update_timestamp).
    effective_at timestamptz,
    occurred_at timestamptz not null,
    recorded_at timestamptz not null default clock_timestamp(),
    -- La de aprobar se registra una vez por solicitud.
    request_id uuid references public.meta_template_requests (id) on delete restrict,
    check ((kind = 'aviso_previo') = (correct_category is not null)),
    check ((source = 'aprobacion') = (request_id is not null)),
    check (source <> 'aprobacion' or kind = 'hecho')
);

create unique index meta_template_category_events_approval_once
    on public.meta_template_category_events (request_id) where request_id is not null;
create index meta_template_category_events_by_instance
    on public.meta_template_category_events (tenant_ref, ally_ref, waba_id, occurred_at desc);

create table public.meta_template_retirements (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    waba_id text not null check (waba_id ~ '^[0-9]{6,32}$'),
    template_name text not null check (template_name ~ '^[a-z0-9][a-z0-9_]{0,249}$'),
    language text not null check (language ~ '^[a-z]{2,3}(_[A-Z]{2})?$'),
    event_id uuid not null references public.meta_template_category_events (id) on delete restrict,
    from_category text not null check (from_category = 'UTILITY'),
    to_category text not null check (to_category in ('MARKETING', 'AUTHENTICATION')),
    retired_at timestamptz not null default clock_timestamp(),
    status text not null default 'retirada' check (status in ('retirada', 'liberada')),
    released_at timestamptz,
    released_by_ref text,
    release_reason text check (release_reason is null or public._flow_store_free_text_is_valid(release_reason)),
    check ((status = 'retirada') = (released_at is null and released_by_ref is null and release_reason is null)),
    check (status = 'retirada' or public._flow_store_actor_is_valid('slack', released_by_ref))
);

create unique index meta_template_retirements_one_live
    on public.meta_template_retirements (tenant_ref, ally_ref, waba_id, template_name, language)
    where status = 'retirada';

create table public.meta_account_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    waba_id text not null check (waba_id ~ '^[0-9]{6,32}$'),
    event text not null check (event ~ '^[A-Z][A-Z_]{1,63}$'),
    violation_type text check (violation_type ~ '^[A-Z][A-Z_]{1,63}$'),
    restriction_types text[] not null default '{}'::text[]
        check (cardinality(restriction_types) <= 10
               and array_to_string(restriction_types, ',') ~ '^([A-Z][A-Z_]{1,63}(,[A-Z][A-Z_]{1,63})*)?$'),
    -- congela, aviso (UNBAN o RECOVERY), repetido (ya habia uno igual en 24 h).
    action text not null check (action in ('congela', 'aviso', 'repetido')),
    occurred_at timestamptz not null,
    recorded_at timestamptz not null default clock_timestamp()
);

create index meta_account_events_by_instance
    on public.meta_account_events (tenant_ref, ally_ref, waba_id, occurred_at desc);

create table public.meta_account_freezes (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    waba_id text not null check (waba_id ~ '^[0-9]{6,32}$'),
    source text not null check (source in ('webhook', 'manual')),
    event_id uuid references public.meta_account_events (id) on delete restrict,
    violation_type text check (violation_type ~ '^[A-Z][A-Z_]{1,63}$'),
    frozen_by_ref text,
    reason text check (reason is null or public._flow_store_free_text_is_valid(reason)),
    frozen_at timestamptz not null default clock_timestamp(),
    status text not null default 'congelada' check (status in ('congelada', 'levantada')),
    lifted_at timestamptz,
    lifted_by_ref text,
    lift_reason text check (lift_reason is null or public._flow_store_free_text_is_valid(lift_reason)),
    check ((source = 'webhook') = (event_id is not null)),
    check ((source = 'manual') = (frozen_by_ref is not null and reason is not null)),
    check (frozen_by_ref is null or public._flow_store_actor_is_valid('slack', frozen_by_ref)),
    check ((status = 'congelada') = (lifted_at is null and lifted_by_ref is null and lift_reason is null)),
    check (status = 'congelada' or public._flow_store_actor_is_valid('slack', lifted_by_ref))
);

create unique index meta_account_freezes_one_live
    on public.meta_account_freezes (tenant_ref, ally_ref, waba_id) where status = 'congelada';

create table public.meta_recategorization_streak_releases (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    waba_id text not null check (waba_id ~ '^[0-9]{6,32}$'),
    released_at timestamptz not null default clock_timestamp(),
    released_by_ref text not null check (public._flow_store_actor_is_valid('slack', released_by_ref)),
    reason text not null check (public._flow_store_free_text_is_valid(reason)),
    streak_at_release integer not null check (streak_at_release >= 0)
);

create table public.meta_enforcement_notices (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    kind text not null check (kind in ('recategorizacion_previa', 'recategorizacion_hecha', 'contador_dos',
                                       'congelamiento', 'restriccion_levantada_por_meta')),
    -- Solo nombres, idiomas y codigos: el texto lo escribe el bridge.
    data jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object' and octet_length(data::text) <= 2048),
    status text not null default 'pendiente'
        check (status in ('pendiente', 'reclamado', 'enviado', 'esperando', 'fallido', 'incierto')),
    attempts integer not null default 0 check (attempts between 0 and 5),
    created_at timestamptz not null default clock_timestamp(),
    claimed_at timestamptz,
    finished_at timestamptz,
    last_error text check (last_error ~ '^[a-z][a-z0-9_:]{0,79}$'),
    check ((status = 'reclamado') = (claimed_at is not null and finished_at is null))
);

create index meta_enforcement_notices_open
    on public.meta_enforcement_notices (tenant_ref, ally_ref, created_at)
    where status in ('pendiente', 'reclamado', 'esperando');

alter table public.meta_template_category_seen enable row level security;
alter table public.meta_template_category_events enable row level security;
alter table public.meta_template_retirements enable row level security;
alter table public.meta_account_events enable row level security;
alter table public.meta_account_freezes enable row level security;
alter table public.meta_recategorization_streak_releases enable row level security;
alter table public.meta_enforcement_notices enable row level security;

-- Lo registrado no se borra ni cambia. Una retirada se libera una vez; un
-- congelamiento se levanta una vez; un aviso solo cambia de estado.
create function public.reject_meta_enforcement_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000', message = 'meta_enforcement_is_permanent';
    end if;
    if tg_table_name = 'meta_template_retirements' then
        if old.status <> 'retirada' or new.status <> 'liberada'
           or (new.id, new.tenant_ref, new.ally_ref, new.waba_id, new.template_name, new.language, new.event_id,
               new.from_category, new.to_category, new.retired_at)
              is distinct from
              (old.id, old.tenant_ref, old.ally_ref, old.waba_id, old.template_name, old.language, old.event_id,
               old.from_category, old.to_category, old.retired_at) then
            raise exception using errcode = '55000', message = 'meta_enforcement_is_permanent';
        end if;
    elsif tg_table_name = 'meta_account_freezes' then
        if old.status <> 'congelada' or new.status <> 'levantada'
           or (new.id, new.tenant_ref, new.ally_ref, new.waba_id, new.source, new.event_id, new.violation_type,
               new.frozen_by_ref, new.reason, new.frozen_at)
              is distinct from
              (old.id, old.tenant_ref, old.ally_ref, old.waba_id, old.source, old.event_id, old.violation_type,
               old.frozen_by_ref, old.reason, old.frozen_at) then
            raise exception using errcode = '55000', message = 'meta_enforcement_is_permanent';
        end if;
    elsif tg_table_name = 'meta_enforcement_notices' then
        if (new.id, new.tenant_ref, new.ally_ref, new.kind, new.data, new.created_at)
           is distinct from (old.id, old.tenant_ref, old.ally_ref, old.kind, old.data, old.created_at) then
            raise exception using errcode = '55000', message = 'meta_enforcement_is_permanent';
        end if;
    elsif tg_table_name <> 'meta_template_category_seen' then
        raise exception using errcode = '55000', message = 'meta_enforcement_is_permanent';
    end if;
    return new;
end;
$function$;

create trigger meta_template_category_events_permanent
    before update or delete on public.meta_template_category_events
    for each row execute function public.reject_meta_enforcement_mutation();
create trigger meta_template_retirements_permanent
    before update or delete on public.meta_template_retirements
    for each row execute function public.reject_meta_enforcement_mutation();
create trigger meta_account_events_permanent
    before update or delete on public.meta_account_events
    for each row execute function public.reject_meta_enforcement_mutation();
create trigger meta_account_freezes_permanent
    before update or delete on public.meta_account_freezes
    for each row execute function public.reject_meta_enforcement_mutation();
create trigger meta_recategorization_streak_releases_permanent
    before update or delete on public.meta_recategorization_streak_releases
    for each row execute function public.reject_meta_enforcement_mutation();
create trigger meta_enforcement_notices_permanent
    before update or delete on public.meta_enforcement_notices
    for each row execute function public.reject_meta_enforcement_mutation();
create trigger meta_template_category_seen_permanent
    before delete on public.meta_template_category_seen
    for each row execute function public.reject_meta_enforcement_mutation();

-- ===========================================================================
-- 2. Lo interno.
-- ===========================================================================

create function public._meta_enforcement_waba(p_waba_id text)
returns text
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    if p_waba_id is null or p_waba_id !~ '^[0-9]{6,32}$' then
        raise exception using errcode = '22023', message = 'meta_enforcement_waba_invalid';
    end if;
    return p_waba_id;
end;
$function$;

-- El contador (D-F1.3): los «hecho» posteriores al ultimo reinicio.
create function public._meta_enforcement_streak(p_tenant_ref text, p_ally_ref text, p_waba_id text)
returns integer
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    with reset as (
        select greatest(
            (select max(r.meta_answered_at) from public.meta_template_requests r
              where r.tenant_ref = p_tenant_ref and r.ally_ref = p_ally_ref
                and r.category_requested = 'UTILITY' and r.meta_category = 'UTILITY'
                and r.meta_answered_at is not null),
            (select max(s.released_at) from public.meta_recategorization_streak_releases s
              where s.tenant_ref = p_tenant_ref and s.ally_ref = p_ally_ref and s.waba_id = p_waba_id)
        ) as at
    )
    select count(*)::integer from public.meta_template_category_events e, reset
     where e.tenant_ref = p_tenant_ref and e.ally_ref = p_ally_ref and e.waba_id = p_waba_id
       and e.kind = 'hecho' and (reset.at is null or e.occurred_at > reset.at);
$function$;

create function public._meta_enforcement_notice(p_tenant_ref text, p_ally_ref text, p_kind text, p_data jsonb)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.meta_enforcement_notices (tenant_ref, ally_ref, kind, data)
    values (p_tenant_ref, p_ally_ref, p_kind, p_data);
$function$;

-- Un cambio de categoria, venga de donde venga. Devuelve que hizo:
-- registrado, retirada, repetido o ignorado.
create function public._meta_enforcement_category_change(
    p_tenant_ref text, p_ally_ref text, p_waba_id text, p_name text, p_language text, p_meta_id text,
    p_kind text, p_source text, p_previous text, p_new text, p_correct text, p_effective_at timestamptz,
    p_occurred_at timestamptz)
returns text
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_event uuid;
    v_before integer;
    v_after integer;
    v_data jsonb;
begin
    if p_kind = 'aviso_previo' then
        if p_correct is null or p_correct = p_new then
            return 'ignorado';
        end if;
        if exists (select 1 from public.meta_template_category_events e
                    where e.tenant_ref = p_tenant_ref and e.ally_ref = p_ally_ref and e.waba_id = p_waba_id
                      and e.template_name = p_name and e.language = p_language and e.kind = 'aviso_previo'
                      and e.correct_category = p_correct and e.recorded_at > clock_timestamp() - interval '7 days') then
            return 'repetido';
        end if;
        insert into public.meta_template_category_events (tenant_ref, ally_ref, waba_id, template_name, language,
            meta_template_id, kind, source, new_category, correct_category, effective_at, occurred_at)
        values (p_tenant_ref, p_ally_ref, p_waba_id, p_name, p_language, p_meta_id, 'aviso_previo', p_source,
                p_new, p_correct, p_effective_at, p_occurred_at);
        perform public._meta_enforcement_notice(p_tenant_ref, p_ally_ref, 'recategorizacion_previa',
            jsonb_build_object('template_name', p_name, 'language', p_language, 'from', p_new, 'to', p_correct,
                               'effective_at', p_effective_at));
        return 'registrado';
    end if;
    -- hecho: solo de utilidad a otra cosa (D-F1.2).
    if p_previous is distinct from 'UTILITY' or p_new not in ('MARKETING', 'AUTHENTICATION') then
        return 'ignorado';
    end if;
    if exists (select 1 from public.meta_template_retirements r
                where r.tenant_ref = p_tenant_ref and r.ally_ref = p_ally_ref and r.waba_id = p_waba_id
                  and r.template_name = p_name and r.language = p_language and r.status = 'retirada')
       or exists (select 1 from public.meta_template_category_events e
                   where e.tenant_ref = p_tenant_ref and e.ally_ref = p_ally_ref and e.waba_id = p_waba_id
                     and e.template_name = p_name and e.language = p_language and e.kind = 'hecho'
                     and e.source <> 'aprobacion' and e.new_category = p_new
                     and e.recorded_at > clock_timestamp() - interval '7 days') then
        return 'repetido';
    end if;
    v_before := public._meta_enforcement_streak(p_tenant_ref, p_ally_ref, p_waba_id);
    insert into public.meta_template_category_events (tenant_ref, ally_ref, waba_id, template_name, language,
        meta_template_id, kind, source, previous_category, new_category, occurred_at)
    values (p_tenant_ref, p_ally_ref, p_waba_id, p_name, p_language, p_meta_id, 'hecho', p_source, p_previous,
            p_new, p_occurred_at)
    returning id into v_event;
    insert into public.meta_template_retirements (tenant_ref, ally_ref, waba_id, template_name, language, event_id,
        from_category, to_category)
    values (p_tenant_ref, p_ally_ref, p_waba_id, p_name, p_language, v_event, p_previous, p_new);
    v_data := jsonb_build_object('template_name', p_name, 'language', p_language, 'from', p_previous, 'to', p_new);
    perform public._meta_enforcement_notice(p_tenant_ref, p_ally_ref, 'recategorizacion_hecha', v_data);
    v_after := public._meta_enforcement_streak(p_tenant_ref, p_ally_ref, p_waba_id);
    if v_before < 2 and v_after >= 2 then
        perform public._meta_enforcement_notice(p_tenant_ref, p_ally_ref, 'contador_dos',
            jsonb_build_object('streak', v_after));
    end if;
    return 'retirada';
end;
$function$;

-- Las solicitudes pedidas como UTILITY que Meta aprobo como otra categoria y
-- que todavia no contaron. Devuelve cuantas sumo.
create function public._meta_enforcement_sweep_approvals(p_tenant_ref text, p_ally_ref text, p_waba_id text)
returns integer
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_request record;
    v_before integer;
    v_count integer := 0;
begin
    v_before := public._meta_enforcement_streak(p_tenant_ref, p_ally_ref, p_waba_id);
    for v_request in
        select r.id, r.template_name, r.language, r.meta_template_id, r.meta_category, r.meta_answered_at
          from public.meta_template_requests r
         where r.tenant_ref = p_tenant_ref and r.ally_ref = p_ally_ref
           and r.category_requested = 'UTILITY' and r.meta_category in ('MARKETING', 'AUTHENTICATION')
           and r.meta_answered_at is not null
           and not exists (select 1 from public.meta_template_category_events e where e.request_id = r.id)
         order by r.meta_answered_at
    loop
        insert into public.meta_template_category_events (tenant_ref, ally_ref, waba_id, template_name, language,
            meta_template_id, kind, source, previous_category, new_category, occurred_at, request_id)
        values (p_tenant_ref, p_ally_ref, p_waba_id, v_request.template_name, v_request.language,
                v_request.meta_template_id, 'hecho', 'aprobacion', 'UTILITY', v_request.meta_category,
                v_request.meta_answered_at, v_request.id);
        v_count := v_count + 1;
    end loop;
    if v_count > 0 and v_before < 2 and public._meta_enforcement_streak(p_tenant_ref, p_ally_ref, p_waba_id) >= 2 then
        perform public._meta_enforcement_notice(p_tenant_ref, p_ally_ref, 'contador_dos',
            jsonb_build_object('streak', public._meta_enforcement_streak(p_tenant_ref, p_ally_ref, p_waba_id)));
    end if;
    return v_count;
end;
$function$;

create function public._meta_enforcement_state(p_tenant_ref text, p_ally_ref text, p_waba_id text)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'frozen', (select jsonb_build_object('freeze_id', f.id, 'source', f.source, 'violation_type', f.violation_type,
                                             'frozen_at', f.frozen_at)
                     from public.meta_account_freezes f
                    where f.tenant_ref = p_tenant_ref and f.ally_ref = p_ally_ref and f.waba_id = p_waba_id
                      and f.status = 'congelada'),
        'retired', coalesce((select jsonb_agg(jsonb_build_object('template_name', r.template_name,
                                 'language', r.language, 'from', r.from_category, 'to', r.to_category,
                                 'retired_at', r.retired_at) order by r.retired_at)
                               from public.meta_template_retirements r
                              where r.tenant_ref = p_tenant_ref and r.ally_ref = p_ally_ref
                                and r.waba_id = p_waba_id and r.status = 'retirada'), '[]'::jsonb),
        'streak', public._meta_enforcement_streak(p_tenant_ref, p_ally_ref, p_waba_id),
        'last_seen_at', (select max(s.seen_at) from public.meta_template_category_seen s
                          where s.tenant_ref = p_tenant_ref and s.ally_ref = p_ally_ref and s.waba_id = p_waba_id));
$function$;

-- ===========================================================================
-- 3. Las funciones de service_role.
-- ===========================================================================

-- El sondeo: la lista entera de plantillas de la cuenta, cada una
-- {"id", "name", "language", "category", "correct_category"}. Compara con lo
-- ultimo visto, registra y devuelve el estado. Una plantilla que no viene no
-- se toca (la lista de Meta es la fuente, no lo que falta en ella).
create function public.record_meta_template_categories_v1(
    p_instance jsonb, p_waba_id text, p_templates jsonb, p_observed_at timestamptz)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_waba text := public._meta_enforcement_waba(p_waba_id);
    v_first boolean;
    v_item jsonb;
    v_seen public.meta_template_category_seen%rowtype;
    v_name text;
    v_language text;
    v_id text;
    v_category text;
    v_correct text;
    v_outcome text;
    v_counts jsonb := '{}'::jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if jsonb_typeof(p_templates) is distinct from 'array' or jsonb_array_length(p_templates) > 6000
       or p_observed_at is null then
        raise exception using errcode = '22023', message = 'meta_enforcement_templates_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'meta_enforcement:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    v_first := not exists (select 1 from public.meta_template_category_seen s
                            where s.tenant_ref = v_instance.o_tenant_ref and s.ally_ref = v_instance.o_ally_ref
                              and s.waba_id = v_waba);
    for v_item in select value from jsonb_array_elements(p_templates) loop
        v_name := v_item ->> 'name';
        v_language := v_item ->> 'language';
        v_id := v_item ->> 'id';
        v_category := v_item ->> 'category';
        v_correct := v_item ->> 'correct_category';
        if jsonb_typeof(v_item) is distinct from 'object'
           or not coalesce(v_name ~ '^[a-z0-9][a-z0-9_]{0,249}$' and v_language ~ '^[a-z]{2,3}(_[A-Z]{2})?$'
                           and v_category in ('UTILITY', 'MARKETING', 'AUTHENTICATION')
                           and (v_id is null or v_id ~ '^[0-9]{1,32}$')
                           and (v_correct is null or v_correct in ('UTILITY', 'MARKETING', 'AUTHENTICATION')), false) then
            v_counts := jsonb_set(v_counts, '{invalida}', to_jsonb(coalesce((v_counts ->> 'invalida')::integer, 0) + 1));
            continue;
        end if;
        select * into v_seen from public.meta_template_category_seen s
         where s.tenant_ref = v_instance.o_tenant_ref and s.ally_ref = v_instance.o_ally_ref and s.waba_id = v_waba
           and s.template_name = v_name and s.language = v_language;
        v_outcome := null;
        if found and v_seen.category <> v_category then
            v_outcome := public._meta_enforcement_category_change(v_instance.o_tenant_ref, v_instance.o_ally_ref,
                v_waba, v_name, v_language, v_id, 'hecho', 'sondeo', v_seen.category, v_category, null, null,
                p_observed_at);
        end if;
        if v_correct is not null and v_correct <> v_category then
            perform public._meta_enforcement_category_change(v_instance.o_tenant_ref, v_instance.o_ally_ref,
                v_waba, v_name, v_language, v_id, 'aviso_previo', 'sondeo', null, v_category, v_correct, null,
                p_observed_at);
        end if;
        insert into public.meta_template_category_seen (tenant_ref, ally_ref, waba_id, template_name, language,
            meta_template_id, category, correct_category, seen_at)
        values (v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba, v_name, v_language, v_id, v_category,
                v_correct, p_observed_at)
        on conflict (tenant_ref, ally_ref, waba_id, template_name, language) do update
           set meta_template_id = excluded.meta_template_id, category = excluded.category,
               correct_category = excluded.correct_category, seen_at = excluded.seen_at;
        if v_outcome is not null then
            v_counts := jsonb_set(v_counts, array[v_outcome],
                                  to_jsonb(coalesce((v_counts ->> v_outcome)::integer, 0) + 1));
        end if;
    end loop;
    v_counts := jsonb_set(v_counts, '{aprobacion}', to_jsonb(public._meta_enforcement_sweep_approvals(
        v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba)));
    return jsonb_build_object('ok', true, 'baseline', v_first, 'counts', v_counts,
        'state', public._meta_enforcement_state(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba));
end;
$function$;

-- Un webhook template_category_update ya leido por el bridge:
-- {"meta_template_id", "name", "language", "kind": "aviso_previo"|"hecho",
--  "previous_category", "new_category", "correct_category", "effective_at", "occurred_at"}.
create function public.record_meta_template_category_webhook_v1(p_instance jsonb, p_waba_id text, p_event jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_waba text := public._meta_enforcement_waba(p_waba_id);
    v_name text := p_event ->> 'name';
    v_language text := p_event ->> 'language';
    v_id text := p_event ->> 'meta_template_id';
    v_kind text := p_event ->> 'kind';
    v_previous text := p_event ->> 'previous_category';
    v_new text := p_event ->> 'new_category';
    v_correct text := p_event ->> 'correct_category';
    v_effective timestamptz;
    v_occurred timestamptz;
    v_outcome text;
begin
    v_instance := public._flow_store_instance(p_instance);
    begin
        v_effective := (p_event ->> 'effective_at')::timestamptz;
        v_occurred := (p_event ->> 'occurred_at')::timestamptz;
    exception when others then
        raise exception using errcode = '22023', message = 'meta_enforcement_event_invalid';
    end;
    if jsonb_typeof(p_event) is distinct from 'object'
       or not coalesce(v_name ~ '^[a-z0-9][a-z0-9_]{0,249}$' and v_language ~ '^[a-z]{2,3}(_[A-Z]{2})?$'
                       and (v_id is null or v_id ~ '^[0-9]{1,32}$') and v_occurred is not null
                       and v_new in ('UTILITY', 'MARKETING', 'AUTHENTICATION')
                       and ((v_kind = 'hecho' and v_previous in ('UTILITY', 'MARKETING', 'AUTHENTICATION')
                             and v_correct is null)
                            or (v_kind = 'aviso_previo' and v_previous is null
                                and v_correct in ('UTILITY', 'MARKETING', 'AUTHENTICATION'))), false) then
        raise exception using errcode = '22023', message = 'meta_enforcement_event_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'meta_enforcement:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    v_outcome := public._meta_enforcement_category_change(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba,
        v_name, v_language, v_id, v_kind, 'webhook', v_previous, v_new, v_correct, v_effective, v_occurred);
    if v_kind = 'hecho' then
        -- Lo visto pasa a la categoria nueva: el sondeo siguiente no lo registra otra vez.
        insert into public.meta_template_category_seen (tenant_ref, ally_ref, waba_id, template_name, language,
            meta_template_id, category, correct_category, seen_at)
        values (v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba, v_name, v_language, v_id, v_new, null,
                v_occurred)
        on conflict (tenant_ref, ally_ref, waba_id, template_name, language) do update
           set category = excluded.category, correct_category = null,
               meta_template_id = coalesce(excluded.meta_template_id, public.meta_template_category_seen.meta_template_id),
               seen_at = greatest(public.meta_template_category_seen.seen_at, excluded.seen_at);
    end if;
    return jsonb_build_object('ok', true, 'outcome', v_outcome,
        'state', public._meta_enforcement_state(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba));
end;
$function$;

-- Un webhook account_update ya leido por el bridge:
-- {"event", "violation_type", "restriction_types": [...], "occurred_at"}.
-- Solo ACCOUNT_RESTRICTION entra; el resto lo descarta el bridge antes.
create function public.record_meta_account_event_v1(p_instance jsonb, p_waba_id text, p_event jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_waba text := public._meta_enforcement_waba(p_waba_id);
    v_event text := p_event ->> 'event';
    v_violation text := p_event ->> 'violation_type';
    v_types text[];
    v_occurred timestamptz;
    v_action text;
    v_event_id uuid;
begin
    v_instance := public._flow_store_instance(p_instance);
    begin
        v_occurred := (p_event ->> 'occurred_at')::timestamptz;
        select coalesce(array_agg(t order by t), '{}'::text[]) into v_types
          from jsonb_array_elements_text(coalesce(p_event -> 'restriction_types', '[]'::jsonb)) t;
    exception when others then
        raise exception using errcode = '22023', message = 'meta_enforcement_event_invalid';
    end;
    if jsonb_typeof(p_event) is distinct from 'object' or v_event is distinct from 'ACCOUNT_RESTRICTION'
       or v_occurred is null or not coalesce(v_violation is null or v_violation ~ '^[A-Z][A-Z_]{1,63}$', false)
       or cardinality(v_types) > 10
       or array_to_string(v_types, ',') !~ '^([A-Z][A-Z_]{1,63}(,[A-Z][A-Z_]{1,63})*)?$' then
        raise exception using errcode = '22023', message = 'meta_enforcement_event_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'meta_enforcement:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    v_action := case when v_violation in ('UTILITY_TEMPLATE_ABUSE_UNBAN', 'UTILITY_TEMPLATE_ABUSE_RATE_LIMIT_RECOVERY')
                     then 'aviso' else 'congela' end;
    -- Meta reintenta la entrega: el mismo evento en 24 h es repetido.
    if exists (select 1 from public.meta_account_events e
                where e.tenant_ref = v_instance.o_tenant_ref and e.ally_ref = v_instance.o_ally_ref
                  and e.waba_id = v_waba and e.event = v_event
                  and e.violation_type is not distinct from v_violation and e.restriction_types = v_types
                  and e.occurred_at = v_occurred) then
        v_action := 'repetido';
    end if;
    insert into public.meta_account_events (tenant_ref, ally_ref, waba_id, event, violation_type, restriction_types,
                                            action, occurred_at)
    values (v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba, v_event, v_violation, v_types, v_action,
            v_occurred)
    returning id into v_event_id;
    if v_action = 'congela' then
        if not exists (select 1 from public.meta_account_freezes f
                        where f.tenant_ref = v_instance.o_tenant_ref and f.ally_ref = v_instance.o_ally_ref
                          and f.waba_id = v_waba and f.status = 'congelada') then
            insert into public.meta_account_freezes (tenant_ref, ally_ref, waba_id, source, event_id, violation_type)
            values (v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba, 'webhook', v_event_id, v_violation);
        end if;
        perform public._meta_enforcement_notice(v_instance.o_tenant_ref, v_instance.o_ally_ref, 'congelamiento',
            jsonb_build_object('source', 'webhook', 'violation_type', v_violation, 'restriction_types', to_jsonb(v_types)));
    elsif v_action = 'aviso' then
        perform public._meta_enforcement_notice(v_instance.o_tenant_ref, v_instance.o_ally_ref,
            'restriccion_levantada_por_meta', jsonb_build_object('violation_type', v_violation));
    end if;
    return jsonb_build_object('ok', true, 'action', v_action,
        'state', public._meta_enforcement_state(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba));
end;
$function$;

create function public.get_meta_enforcement_state_v1(p_instance jsonb, p_waba_id text)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_waba text := public._meta_enforcement_waba(p_waba_id);
begin
    v_instance := public._flow_store_instance(p_instance);
    return public._meta_enforcement_state(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba);
end;
$function$;

-- Lo que hace una persona (el bridge chequea que este en la lista de
-- aprobadores): congelar, descongelar, liberar_plantilla
-- ({"template_name", "language"}) y liberar_contador. Repetir contesta
-- replayed sin cambiar nada.
create function public.act_on_meta_enforcement_v1(
    p_instance jsonb, p_waba_id text, p_action text, p_target jsonb, p_reason text, p_actor_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_waba text := public._meta_enforcement_waba(p_waba_id);
    v_replayed boolean := false;
    v_count integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('slack', p_actor_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'meta_enforcement_reason_invalid';
    end if;
    if p_action is null or p_action not in ('congelar', 'descongelar', 'liberar_plantilla', 'liberar_contador') then
        raise exception using errcode = '22023', message = 'meta_enforcement_action_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'meta_enforcement:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    if p_action = 'congelar' then
        if exists (select 1 from public.meta_account_freezes f
                    where f.tenant_ref = v_instance.o_tenant_ref and f.ally_ref = v_instance.o_ally_ref
                      and f.waba_id = v_waba and f.status = 'congelada') then
            v_replayed := true;
        else
            insert into public.meta_account_freezes (tenant_ref, ally_ref, waba_id, source, frozen_by_ref, reason)
            values (v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba, 'manual', p_actor_ref, btrim(p_reason));
            perform public._meta_enforcement_notice(v_instance.o_tenant_ref, v_instance.o_ally_ref, 'congelamiento',
                jsonb_build_object('source', 'manual'));
        end if;
    elsif p_action = 'descongelar' then
        update public.meta_account_freezes
           set status = 'levantada', lifted_at = clock_timestamp(), lifted_by_ref = p_actor_ref,
               lift_reason = btrim(p_reason)
         where tenant_ref = v_instance.o_tenant_ref and ally_ref = v_instance.o_ally_ref and waba_id = v_waba
           and status = 'congelada';
        get diagnostics v_count = row_count;
        v_replayed := v_count = 0;
    elsif p_action = 'liberar_plantilla' then
        if jsonb_typeof(p_target) is distinct from 'object'
           or not coalesce((p_target ->> 'template_name') ~ '^[a-z0-9][a-z0-9_]{0,249}$'
                           and (p_target ->> 'language') ~ '^[a-z]{2,3}(_[A-Z]{2})?$', false) then
            raise exception using errcode = '22023', message = 'meta_enforcement_target_invalid';
        end if;
        update public.meta_template_retirements
           set status = 'liberada', released_at = clock_timestamp(), released_by_ref = p_actor_ref,
               release_reason = btrim(p_reason)
         where tenant_ref = v_instance.o_tenant_ref and ally_ref = v_instance.o_ally_ref and waba_id = v_waba
           and template_name = p_target ->> 'template_name' and language = p_target ->> 'language'
           and status = 'retirada';
        get diagnostics v_count = row_count;
        if v_count = 0 then
            raise exception using errcode = 'P0002', message = 'meta_enforcement_template_not_retired';
        end if;
    else
        v_count := public._meta_enforcement_streak(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba);
        if v_count = 0 then
            v_replayed := true;
        else
            insert into public.meta_recategorization_streak_releases (tenant_ref, ally_ref, waba_id,
                released_by_ref, reason, streak_at_release)
            values (v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba, p_actor_ref, btrim(p_reason), v_count);
        end if;
    end if;
    return jsonb_build_object('ok', true, 'replayed', v_replayed,
        'state', public._meta_enforcement_state(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_waba));
end;
$function$;

-- Los avisos que tocan, de a p_limit (1 a 20). Un reclamo de hace mas de 5
-- minutos sin cerrar pasa a incierto y no se repite (puede haber llegado).
create function public.claim_meta_enforcement_notices_v1(p_instance jsonb, p_limit integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_claimed jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_limit is null or p_limit < 1 or p_limit > 20 then
        raise exception using errcode = '22023', message = 'meta_enforcement_limit_invalid';
    end if;
    update public.meta_enforcement_notices
       set status = 'incierto', finished_at = clock_timestamp(), claimed_at = null,
           last_error = 'claim_expired'
     where tenant_ref = v_instance.o_tenant_ref and ally_ref = v_instance.o_ally_ref and status = 'reclamado'
       and claimed_at < clock_timestamp() - interval '5 minutes';
    with picked as (
        select n.id from public.meta_enforcement_notices n
         where n.tenant_ref = v_instance.o_tenant_ref and n.ally_ref = v_instance.o_ally_ref
           and n.status in ('pendiente', 'esperando')
         order by n.created_at
         limit p_limit
         for update skip locked
    ), claimed as (
        update public.meta_enforcement_notices n
           set status = 'reclamado', claimed_at = clock_timestamp(), finished_at = null,
               attempts = least(n.attempts + 1, 5)
          from picked where n.id = picked.id
        returning n.id, n.kind, n.data, n.attempts, n.created_at
    )
    select coalesce(jsonb_agg(jsonb_build_object('notice_id', c.id, 'kind', c.kind, 'data', c.data,
                                                 'attempts', c.attempts) order by c.created_at), '[]'::jsonb)
      into v_claimed from claimed c;
    return jsonb_build_object('notices', v_claimed,
        'counts', coalesce((select jsonb_object_agg(s.status, s.n) from (
            select n.status, count(*) as n from public.meta_enforcement_notices n
             where n.tenant_ref = v_instance.o_tenant_ref and n.ally_ref = v_instance.o_ally_ref
             group by n.status) s), '{}'::jsonb));
end;
$function$;

-- Cerrar un aviso reclamado: sent, retry (vuelve a pendiente; al quinto
-- intento, fallido), waiting (espera sin gastar el intento), unknown o failed.
create function public.finish_meta_enforcement_notice_v1(
    p_instance jsonb, p_notice_id uuid, p_outcome text, p_error_code text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_notice public.meta_enforcement_notices%rowtype;
    v_status text;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_outcome is null or p_outcome not in ('sent', 'retry', 'waiting', 'unknown', 'failed')
       or not coalesce(p_error_code is null or p_error_code ~ '^[a-z][a-z0-9_:]{0,79}$', false) then
        raise exception using errcode = '22023', message = 'meta_enforcement_outcome_invalid';
    end if;
    select * into v_notice from public.meta_enforcement_notices n
     where n.id = p_notice_id and n.tenant_ref = v_instance.o_tenant_ref and n.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'meta_enforcement_notice_not_found';
    end if;
    if v_notice.status <> 'reclamado' then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_enforcement_notice_not_claimed',
                                  'status', v_notice.status);
    end if;
    v_status := case p_outcome
        when 'sent' then 'enviado'
        when 'retry' then case when v_notice.attempts >= 5 then 'fallido' else 'pendiente' end
        when 'waiting' then 'esperando'
        when 'unknown' then 'incierto'
        else 'fallido' end;
    update public.meta_enforcement_notices
       set status = v_status, claimed_at = null, finished_at = clock_timestamp(), last_error = p_error_code,
           attempts = case when p_outcome = 'waiting' then greatest(attempts - 1, 0) else attempts end
     where id = v_notice.id;
    return jsonb_build_object('ok', true, 'status', v_status);
end;
$function$;

-- ===========================================================================
-- 4. Los permisos. Las tablas, para nadie de la API. Las internas y el
--    disparador, para nadie. Las publicas, solo para service_role.
-- ===========================================================================

do $roles$
declare
    v_table text;
    v_function regprocedure;
    v_role text;
begin
    foreach v_table in array array['meta_template_category_seen', 'meta_template_category_events',
                                   'meta_template_retirements', 'meta_account_events', 'meta_account_freezes',
                                   'meta_recategorization_streak_releases', 'meta_enforcement_notices'] loop
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
        'public.reject_meta_enforcement_mutation()',
        'public._meta_enforcement_waba(text)',
        'public._meta_enforcement_streak(text,text,text)',
        'public._meta_enforcement_notice(text,text,text,jsonb)',
        'public._meta_enforcement_category_change(text,text,text,text,text,text,text,text,text,text,text,timestamptz,timestamptz)',
        'public._meta_enforcement_sweep_approvals(text,text,text)',
        'public._meta_enforcement_state(text,text,text)',
        'public.record_meta_template_categories_v1(jsonb,text,jsonb,timestamptz)',
        'public.record_meta_template_category_webhook_v1(jsonb,text,jsonb)',
        'public.record_meta_account_event_v1(jsonb,text,jsonb)',
        'public.get_meta_enforcement_state_v1(jsonb,text)',
        'public.act_on_meta_enforcement_v1(jsonb,text,text,jsonb,text,text)',
        'public.claim_meta_enforcement_notices_v1(jsonb,integer)',
        'public.finish_meta_enforcement_notice_v1(jsonb,uuid,text,text)'
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
    if to_regrole('service_role') is not null then
        foreach v_function in array array[
            'public.record_meta_template_categories_v1(jsonb,text,jsonb,timestamptz)',
            'public.record_meta_template_category_webhook_v1(jsonb,text,jsonb)',
            'public.record_meta_account_event_v1(jsonb,text,jsonb)',
            'public.get_meta_enforcement_state_v1(jsonb,text)',
            'public.act_on_meta_enforcement_v1(jsonb,text,text,jsonb,text,text)',
            'public.claim_meta_enforcement_notices_v1(jsonb,integer)',
            'public.finish_meta_enforcement_notice_v1(jsonb,uuid,text,text)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to %I', v_function, 'service_role');
        end loop;
    end if;
end;
$roles$;

commit;
