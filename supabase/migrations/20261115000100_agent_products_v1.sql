-- product-card-v1: el producto que suma el Automation Expert como dato (E-569,
-- MP-07 del plan de multiproducto). Contrato: docs/contracts/product-card-v1.md.
--
-- Por que. Hoy un producto que no es el principal se declara en [[productos]]
-- del manifiesto, por PR, y se siembra a mano. El plan de multiproducto (D1 de
-- Dan del 2026-11-13) quiere que el usuario lo sume solo con el AE y la pagina
-- de aprobacion. Esta migracion es el almacen: el AE guarda el producto como
-- borrador (el minimo validado por el bridge + el texto libre + las
-- advertencias del esqueleto), lo valida y la base lo congela 24 horas con su
-- huella. Publicar, pausar y cerrar, con la siembra, son de MP-08.
--
-- 1. Dos tablas nuevas, vacias: agent_products (una fila por producto y
--    version) y agent_product_events (solo agregar). Sin permisos para nadie de
--    la API, con RLS.
-- 2. Cuatro funciones del Automation Expert, solo para automation_expert_change
--    (20261021000100): proponer (guardar o rehacer un borrador), registrar la
--    validacion, obtener un producto y listarlos. Ninguna publica.
--
-- La base no corre las reglas del contrato (las corre el bridge,
-- agent_products.py): mira la forma, para que una fila no pueda traer otra
-- cosa. El listado devuelve tambien la identidad de los productos no descartados
-- (clave, nombre, alias, producto de Hotmart, ofertas e inclusiones), que es lo
-- que el bridge necesita para rechazar una clave, un ID, una oferta o un alias
-- que ya usa otro producto.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- AGENT_PRODUCTS_ENABLED el bridge no llama a ninguna de estas funciones.
-- Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe o no es de esta instancia;
-- 55000 un estado que no lo permite. Los resultados del ciclo (revision vieja,
-- vencida, tope...) vuelven como {"ok": false, "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_store_actor_is_valid(text,text)') is null
       or to_regprocedure('public._flow_store_free_text_is_valid(text)') is null
       or to_regprocedure('public.set_updated_at()') is null then
        raise exception using errcode = '55000', message = 'agent_products_prerequisite_missing';
    end if;
    if to_regclass('public.agent_products') is not null then
        raise exception using errcode = '55000', message = 'agent_products_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. La forma que usan los CHECK.
-- ===========================================================================

-- El minimo normalizado que guarda el bridge (agent_products.py). Las claves
-- exactas, los tipos y los topes; no las reglas contra la instancia.
create function public._agent_product_minimum_is_valid(p_minimum jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
declare
    v_keys text[];
begin
    if jsonb_typeof(p_minimum) is distinct from 'object' or octet_length(p_minimum::text) > 32768 then
        return false;
    end if;
    select array_agg(k order by k) into v_keys from jsonb_object_keys(p_minimum) k;
    if v_keys is distinct from array['alias', 'clave', 'hotmart', 'no_se_puede_prometer', 'nombre', 'ofertas',
                                     'relaciones'] then
        return false;
    end if;
    return coalesce(
        jsonb_typeof(p_minimum -> 'clave') = 'string'
        and p_minimum ->> 'clave' ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
        and length(p_minimum ->> 'clave') <= 40
        and p_minimum ->> 'clave' not in ('principal', 'todos', 'ninguno')
        and jsonb_typeof(p_minimum -> 'nombre') = 'string'
        and length(p_minimum ->> 'nombre') between 1 and 80
        and jsonb_typeof(p_minimum -> 'alias') = 'array'
        and jsonb_array_length(p_minimum -> 'alias') between 1 and 12
        and not exists (select 1 from jsonb_array_elements(p_minimum -> 'alias') a
                         where jsonb_typeof(a) is distinct from 'string' or length(a #>> '{}') not between 3 and 60)
        and jsonb_typeof(p_minimum -> 'hotmart') = 'object'
        and (select array_agg(k order by k) from jsonb_object_keys(p_minimum -> 'hotmart') k)
            = array['hotlink', 'producto_id']
        and jsonb_typeof(p_minimum -> 'hotmart' -> 'producto_id') = 'number'
        and jsonb_typeof(p_minimum -> 'hotmart' -> 'hotlink') = 'string'
        and (p_minimum -> 'hotmart' ->> 'producto_id') ~ '^[1-9][0-9]{0,11}$'
        and p_minimum -> 'hotmart' ->> 'hotlink' ~ '^[A-Za-z0-9]{4,32}$'
        and jsonb_typeof(p_minimum -> 'ofertas') = 'array'
        and jsonb_array_length(p_minimum -> 'ofertas') between 1 and 20
        and not exists (select 1 from jsonb_array_elements(p_minimum -> 'ofertas') o
                         where jsonb_typeof(o) is distinct from 'object'
                            or jsonb_typeof(o -> 'codigo') is distinct from 'string'
                            or jsonb_typeof(o -> 'precio') is distinct from 'string'
                            or jsonb_typeof(o -> 'moneda') is distinct from 'string'
                            or not coalesce(o ->> 'codigo' ~ '^[A-Za-z0-9]{4,32}$', false)
                            or not coalesce(o ->> 'precio' ~ '^[0-9]{1,9}(\.[0-9]{1,2})?$', false)
                            or not coalesce(o ->> 'moneda' ~ '^[A-Z]{3}$', false)
                            or jsonb_typeof(o -> 'por_defecto') is distinct from 'boolean'
                            or jsonb_typeof(o -> 'otros_montos') is distinct from 'array'
                            or jsonb_array_length(o -> 'otros_montos') > 12)
        and (select count(*) from jsonb_array_elements(p_minimum -> 'ofertas') o
              where (o ->> 'por_defecto')::boolean) = 1
        and jsonb_typeof(p_minimum -> 'no_se_puede_prometer') = 'array'
        and jsonb_array_length(p_minimum -> 'no_se_puede_prometer') between 1 and 20
        and jsonb_typeof(p_minimum -> 'relaciones') = 'object'
        and (select array_agg(k order by k) from jsonb_object_keys(p_minimum -> 'relaciones') k)
            = array['incluye_productos', 'no_se_vende_si_tiene', 'venta_cruzada']
        and jsonb_typeof(p_minimum -> 'relaciones' -> 'incluye_productos') = 'array'
        and jsonb_typeof(p_minimum -> 'relaciones' -> 'no_se_vende_si_tiene') = 'array'
        and jsonb_typeof(p_minimum -> 'relaciones' -> 'venta_cruzada') = 'array', false);
end;
$function$;

-- Las advertencias del esqueleto (o lo que devuelva el validador): una lista de
-- {code, field?, detail?} con codigos de la forma de siempre.
create function public._agent_product_findings_are_valid(p_findings jsonb)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(jsonb_typeof(p_findings) = 'array'
        and jsonb_array_length(p_findings) <= 100
        and octet_length(p_findings::text) <= 32768
        and not exists (select 1 from jsonb_array_elements(p_findings) f
                         where jsonb_typeof(f) is distinct from 'object'
                            or not coalesce(f ->> 'code' ~ '^[a-z][a-z0-9_]{1,79}$', false)), false)
$function$;

-- ===========================================================================
-- 2. Las tablas.
-- ===========================================================================

create table public.agent_products (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    status text not null check (status in (
        'borrador', 'validada', 'publicada', 'pausada', 'cerrada', 'vencida', 'descartada')),
    revision integer not null check (revision > 0),
    product_key text not null check (product_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$' and length(product_key) <= 40),
    -- La version del producto: la clave y la version nombran una ficha.
    product_version integer not null check (product_version > 0),
    hotmart_product_id bigint not null check (hotmart_product_id > 0),
    minimum jsonb not null check (public._agent_product_minimum_is_valid(minimum)),
    free_text text not null check (octet_length(free_text) between 1 and 5500),
    warnings jsonb not null default '[]'::jsonb check (public._agent_product_findings_are_valid(warnings)),
    purpose text not null check (public._flow_store_free_text_is_valid(purpose)),
    requested_by_kind text not null check (requested_by_kind = 'automation_expert'),
    requested_by_ref text not null,
    validation jsonb check (validation is null or (jsonb_typeof(validation) = 'object'
        and octet_length(validation::text) <= 131072)),
    fingerprint text check (fingerprint ~ '^[0-9a-f]{64}$'),
    validated_at timestamptz,
    expires_at timestamptz,
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
    constraint agent_products_requested_by
        check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    constraint agent_products_approved_by
        check ((approved_by_kind is null and approved_by_ref is null)
               or public._flow_store_actor_is_valid(approved_by_kind, approved_by_ref)),
    constraint agent_products_paused_by
        check (paused_by_ref is null or public._flow_store_actor_is_valid('slack', paused_by_ref)),
    constraint agent_products_closed_by
        check ((closed_by_kind is null and closed_by_ref is null)
               or public._flow_store_actor_is_valid(closed_by_kind, closed_by_ref)),
    constraint agent_products_identity_matches_minimum
        check (minimum ->> 'clave' = product_key
               and (minimum -> 'hotmart' ->> 'producto_id')::bigint = hotmart_product_id),
    -- El CASE entero adentro de coalesce: una comparacion con NULL no deja pasar la fila.
    constraint agent_products_state_coherent check (coalesce(case status
        when 'borrador' then fingerprint is null and validated_at is null and expires_at is null
            and approved_at is null and approved_by_kind is null and paused_at is null and closed_at is null
        when 'validada' then fingerprint is not null and validation is not null and validated_at is not null
            and expires_at > validated_at and approved_at is null and paused_at is null and closed_at is null
        when 'publicada' then fingerprint is not null and approved_at is not null
            and approved_by_kind is not null and paused_at is null and closed_at is null
        when 'pausada' then fingerprint is not null and approved_at is not null
            and approved_by_kind is not null and paused_at is not null and paused_by_ref is not null
            and closed_at is null
        when 'cerrada' then approved_at is not null and closed_at is not null and closed_by_kind is not null
        when 'vencida' then fingerprint is not null and expires_at is not null and approved_at is null
            and closed_at is not null and closed_by_kind = 'sistema'
        when 'descartada' then approved_at is null and closed_at is not null and closed_by_kind is not null
            and close_reason is not null
    end, false))
);

-- El id es un uuid y no un identity: una secuencia recibiria permisos por los
-- privilegios por defecto de Supabase.
create table public.agent_product_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    product_id uuid not null references public.agent_products (id) on delete restrict,
    event_type text not null check (event_type in (
        'agent_product_draft_saved', 'agent_product_validation_blocked', 'agent_product_validated',
        'agent_product_published', 'agent_product_publish_refused', 'agent_product_paused',
        'agent_product_resumed', 'agent_product_closed', 'agent_product_discarded', 'agent_product_expired')),
    actor_kind text not null check (actor_kind in ('automation_expert', 'slack', 'operador', 'sistema')),
    actor_ref text not null,
    reason_code text not null check (reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'),
    data jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object'
        and octet_length(data::text) <= 16384),
    created_at timestamptz not null default clock_timestamp(),
    constraint agent_product_events_actor
        check (public._flow_store_actor_is_valid(actor_kind, actor_ref))
);

-- Una clave con su version nombra una sola ficha.
create unique index agent_products_key_version_once
    on public.agent_products (tenant_ref, ally_ref, product_key, product_version);
-- Una sola version publicada por producto (la publicacion es de MP-08; el
-- indice ya esta para que ninguna funcion futura pueda dejar dos).
create unique index agent_products_one_live_per_key
    on public.agent_products (tenant_ref, ally_ref, product_key) where status = 'publicada';
create index agent_products_instance_time_idx
    on public.agent_products (tenant_ref, ally_ref, created_at desc);
create index agent_products_open_idx on public.agent_products (tenant_ref, ally_ref)
    where status in ('borrador', 'validada');
create index agent_product_events_product_time_idx
    on public.agent_product_events (product_id, created_at);

alter table public.agent_products enable row level security;
alter table public.agent_product_events enable row level security;

-- ===========================================================================
-- 3. Los disparadores.
-- ===========================================================================

create function public.reject_agent_product_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'agent_product_events' then 'agent_product_event_is_append_only'
        else 'agent_product_is_permanent'
    end;
end;
$function$;

-- El recorrido. Desde validada el contenido queda congelado (volver a borrador
-- es rehacerlo, con otra revision); desde publicada, tambien la aprobacion; un
-- estado final no acepta ningun UPDATE.
create function public.validate_agent_product_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if old.status in ('cerrada', 'vencida', 'descartada') then
        raise exception using errcode = '55000', message = 'agent_product_frozen';
    end if;
    if row(new.id, new.tenant_ref, new.ally_ref, new.product_key, new.product_version, new.requested_by_kind,
           new.requested_by_ref, new.created_at)
       is distinct from row(old.id, old.tenant_ref, old.ally_ref, old.product_key, old.product_version,
                            old.requested_by_kind, old.requested_by_ref, old.created_at) then
        raise exception using errcode = '55000', message = 'agent_product_identity_is_immutable';
    end if;
    if new.status is distinct from old.status and not (
        (old.status = 'borrador' and new.status in ('validada', 'descartada'))
        or (old.status = 'validada' and new.status in ('borrador', 'publicada', 'descartada', 'vencida'))
        or (old.status = 'publicada' and new.status in ('pausada', 'cerrada'))
        or (old.status = 'pausada' and new.status in ('publicada', 'cerrada'))
    ) then
        raise exception using errcode = '23514', message = 'agent_product_transition_invalid';
    end if;
    if old.status = 'borrador' or (old.status = 'validada' and new.status = 'borrador') then
        if new.status = 'borrador'
           and row(new.minimum, new.free_text, new.purpose)
               is distinct from row(old.minimum, old.free_text, old.purpose)
           and new.revision is distinct from old.revision + 1 then
            raise exception using errcode = '23514', message = 'agent_product_revision_must_increment';
        end if;
        if old.status = 'borrador' and new.status <> 'borrador'
           and row(new.minimum, new.free_text, new.purpose, new.revision)
               is distinct from row(old.minimum, old.free_text, old.purpose, old.revision) then
            raise exception using errcode = '55000', message = 'agent_product_frozen';
        end if;
    else
        if row(new.minimum, new.free_text, new.warnings, new.purpose, new.revision, new.hotmart_product_id,
               new.fingerprint, new.validated_at, new.validation)
           is distinct from row(old.minimum, old.free_text, old.warnings, old.purpose, old.revision,
               old.hotmart_product_id, old.fingerprint, old.validated_at, old.validation) then
            raise exception using errcode = '55000', message = 'agent_product_frozen';
        end if;
        if old.status in ('publicada', 'pausada')
           and row(new.approved_at, new.approved_by_kind, new.approved_by_ref)
               is distinct from row(old.approved_at, old.approved_by_kind, old.approved_by_ref) then
            raise exception using errcode = '55000', message = 'agent_product_frozen';
        end if;
    end if;
    return new;
end;
$function$;

create trigger agent_products_validate
before update on public.agent_products
for each row execute function public.validate_agent_product_change();
create trigger agent_products_permanent
before delete on public.agent_products
for each row execute function public.reject_agent_product_mutation();
create trigger agent_products_no_truncate
before truncate on public.agent_products
for each statement execute function public.reject_agent_product_mutation();
create trigger agent_products_set_updated_at
before update on public.agent_products
for each row execute function public.set_updated_at();
create trigger agent_product_events_append_only
before update or delete on public.agent_product_events
for each row execute function public.reject_agent_product_mutation();
create trigger agent_product_events_no_truncate
before truncate on public.agent_product_events
for each statement execute function public.reject_agent_product_mutation();

-- ===========================================================================
-- 4. Los helpers internos (security invoker, de nadie de la API).
-- ===========================================================================

create function public._agent_product_event(
    p_product public.agent_products, p_event_type text, p_actor_kind text, p_actor_ref text,
    p_reason_code text, p_data jsonb)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.agent_product_events (tenant_ref, ally_ref, product_id, event_type,
        actor_kind, actor_ref, reason_code, data)
    values (p_product.tenant_ref, p_product.ally_ref, p_product.id, p_event_type,
        p_actor_kind, p_actor_ref, p_reason_code, coalesce(p_data, '{}'::jsonb))
$function$;

-- La huella la calcula solo la base, sobre el texto del jsonb, al congelar.
create function public._agent_product_fingerprint(
    p_product_id uuid, p_revision integer, p_product_version integer, p_minimum jsonb, p_free_text text,
    p_purpose text)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select encode(sha256(convert_to(jsonb_build_object(
        'contract', 'product-card-v1', 'product_id', p_product_id, 'revision', p_revision,
        'product_version', p_product_version, 'minimum', p_minimum, 'free_text', p_free_text,
        'purpose', p_purpose)::text, 'UTF8')), 'hex')
$function$;

-- Con la fila ya bloqueada: si esta validada y vencio, la pasa a vencida con
-- sistema/vencimiento y deja su evento. No hay barrido.
create function public._agent_product_expire_if_due(p_product public.agent_products)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_product.status is distinct from 'validada'
       or not coalesce(clock_timestamp() >= p_product.expires_at, false) then
        return false;
    end if;
    update public.agent_products
       set status = 'vencida', closed_at = clock_timestamp(),
           closed_by_kind = 'sistema', closed_by_ref = 'vencimiento'
     where id = p_product.id;
    perform public._agent_product_event(p_product, 'agent_product_expired', 'sistema', 'vencimiento',
        'agent_product_expired', jsonb_build_object('expires_at', p_product.expires_at));
    return true;
end;
$function$;

-- Lo que se muestra de un producto al agente. Nunca quien lo pidio ni quien lo
-- aprobo (ningun ID de Slack).
create function public._agent_product_view(p_product public.agent_products)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'product_id', p_product.id,
        'status', p_product.status,
        'effective_status', case
            when p_product.status = 'validada' and clock_timestamp() >= p_product.expires_at then 'vencida'
            else p_product.status end,
        'revision', p_product.revision,
        'product_key', p_product.product_key,
        'product_version', p_product.product_version,
        'minimo', p_product.minimum,
        'texto_libre', p_product.free_text,
        'advertencias', p_product.warnings,
        'purpose', p_product.purpose,
        'validation', p_product.validation,
        'fingerprint', p_product.fingerprint,
        'validated_at', p_product.validated_at,
        'expires_at', p_product.expires_at,
        'approved_at', p_product.approved_at,
        'paused_at', p_product.paused_at,
        'closed_at', p_product.closed_at,
        'closed_by_kind', p_product.closed_by_kind,
        'created_at', p_product.created_at,
        'events', coalesce((
            select jsonb_agg(jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                'reason_code', e.reason_code, 'created_at', e.created_at)
                             order by e.created_at desc, e.id)
              from (select * from public.agent_product_events e
                     where e.product_id = p_product.id
                     order by e.created_at desc, e.id limit 50) e), '[]'::jsonb))
$function$;

-- Los abiertos del agente: un borrador o una validada sin vencer.
create function public._agent_products_open(p_tenant_ref text, p_ally_ref text)
returns integer
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select count(*)::integer from public.agent_products p
     where p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref
       and (p.status = 'borrador' or (p.status = 'validada' and clock_timestamp() < p.expires_at))
$function$;

-- ===========================================================================
-- 5. Las cuatro del Automation Expert.
-- ===========================================================================

-- Proponer: guardar o rehacer un borrador. Sin p_product_id crea la version
-- siguiente de la clave (la primera, si la clave es nueva). Rehacer tambien
-- vale sobre una validada que nadie publico; la huella se borra, asi que una
-- pagina abierta antes ya no puede publicarla.
create function public.save_agent_product_draft_v1(
    p_instance jsonb, p_product_id uuid, p_expected_revision integer, p_minimum jsonb, p_free_text text,
    p_warnings jsonb, p_purpose text, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_product public.agent_products%rowtype;
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
    if not coalesce(public._agent_product_minimum_is_valid(p_minimum), false) then
        raise exception using errcode = '22023', message = 'agent_product_minimum_invalid';
    end if;
    if p_free_text is null or octet_length(p_free_text) not between 1 and 5500 then
        raise exception using errcode = '22023', message = 'agent_product_free_text_invalid';
    end if;
    if not coalesce(public._agent_product_findings_are_valid(p_warnings), false) then
        raise exception using errcode = '22023', message = 'agent_product_warnings_invalid';
    end if;
    if (p_product_id is null) is distinct from (p_expected_revision is null) then
        raise exception using errcode = '22023', message = 'agent_product_redo_invalid';
    end if;

    -- Un lock por instancia: dos borradores a la vez no pasan el tope ni toman la misma version.
    perform set_config('lock_timeout', '5s', true);
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_products:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));

    if p_product_id is null then
        v_open := public._agent_products_open(v_instance.o_tenant_ref, v_instance.o_ally_ref);
        if v_open >= 5 then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_product_open_limit', 'open', v_open);
        end if;
        select coalesce(max(p.product_version), 0) + 1 into v_version from public.agent_products p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and p.product_key = p_minimum ->> 'clave';
        insert into public.agent_products (tenant_ref, ally_ref, status, revision, product_key, product_version,
            hotmart_product_id, minimum, free_text, warnings, purpose, requested_by_kind, requested_by_ref)
        values (v_instance.o_tenant_ref, v_instance.o_ally_ref, 'borrador', 1, p_minimum ->> 'clave', v_version,
            (p_minimum -> 'hotmart' ->> 'producto_id')::bigint, p_minimum, p_free_text, p_warnings, p_purpose,
            'automation_expert', p_requested_by_ref)
        returning * into v_product;
    else
        select * into v_product from public.agent_products p
         where p.id = p_product_id and p.tenant_ref = v_instance.o_tenant_ref
           and p.ally_ref = v_instance.o_ally_ref
         for update;
        if not found then
            raise exception using errcode = 'P0002', message = 'agent_product_not_found';
        end if;
        if public._agent_product_expire_if_due(v_product) then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_product_expired');
        end if;
        if v_product.status not in ('borrador', 'validada') then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_product_not_draft',
                'status', v_product.status);
        end if;
        if v_product.revision is distinct from p_expected_revision then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_product_revision_mismatch',
                'revision', v_product.revision);
        end if;
        if v_product.revision >= 100 then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_product_revision_limit');
        end if;
        if v_product.product_key is distinct from p_minimum ->> 'clave' then
            return jsonb_build_object('ok', false, 'reason_code', 'agent_product_key_is_immutable');
        end if;
        update public.agent_products
           set status = 'borrador', minimum = p_minimum, free_text = p_free_text, warnings = p_warnings,
               purpose = p_purpose, revision = revision + 1,
               hotmart_product_id = (p_minimum -> 'hotmart' ->> 'producto_id')::bigint,
               validation = null, fingerprint = null, validated_at = null, expires_at = null
         where id = v_product.id
        returning * into v_product;
    end if;

    perform public._agent_product_event(v_product, 'agent_product_draft_saved', 'automation_expert',
        p_requested_by_ref, 'agent_product_draft_saved', jsonb_build_object(
            'revision', v_product.revision,
            'product_version', v_product.product_version,
            'minimum_sha256', encode(sha256(convert_to(p_minimum::text, 'UTF8')), 'hex'),
            'free_text_sha256', encode(sha256(convert_to(p_free_text, 'UTF8')), 'hex'),
            'purpose_sha256', encode(sha256(convert_to(p_purpose, 'UTF8')), 'hex'),
            'warnings', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                           from jsonb_array_elements(p_warnings) f)));
    return jsonb_build_object('ok', true, 'product_id', v_product.id, 'revision', v_product.revision,
        'product_version', v_product.product_version, 'status', v_product.status);
end;
$function$;

-- Registrar la validacion que corrio el bridge. La base no corre las reglas:
-- le cree a quien registra el resultado, pero arma ella la huella. Sin
-- bloqueos, congela el producto 24 horas y guarda las advertencias que dio la
-- validacion.
create function public.record_agent_product_validation_v1(
    p_instance jsonb, p_product_id uuid, p_revision integer, p_validator jsonb, p_result jsonb,
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
    v_product public.agent_products%rowtype;
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
        and p_validator ->> 'contract' = 'product-card-v1'
        and jsonb_typeof(p_validator -> 'rules_version') = 'number'
        and p_validator ->> 'rules_version' ~ '^[1-9][0-9]{0,3}$'
        and p_validator ->> 'product_version' ~ '^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
        and p_validator ->> 'git_sha' ~ '^[0-9a-f]{7,40}$', false) then
        raise exception using errcode = '22023', message = 'agent_product_validator_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_result) = 'object'
        and (select array_agg(k order by k) from jsonb_object_keys(p_result) k) = array['blocks', 'warnings']
        and public._agent_product_findings_are_valid(p_result -> 'blocks')
        and public._agent_product_findings_are_valid(p_result -> 'warnings'), false) then
        raise exception using errcode = '22023', message = 'agent_product_result_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_preview) = 'object' and octet_length(p_preview::text) <= 65536, false) then
        raise exception using errcode = '22023', message = 'agent_product_preview_invalid';
    end if;
    if p_revision is null then
        raise exception using errcode = '22023', message = 'agent_product_revision_invalid';
    end if;

    select * into v_product from public.agent_products p
     where p.id = p_product_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_product_not_found';
    end if;
    select count(*) into v_validations from public.agent_product_events e
     where e.product_id = v_product.id
       and e.event_type in ('agent_product_validated', 'agent_product_validation_blocked');
    if v_validations >= 100 then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_validation_limit');
    end if;
    if v_product.status is distinct from 'borrador' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_not_draft', 'status', v_product.status);
    end if;
    if v_product.revision is distinct from p_revision then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_revision_mismatch',
            'revision', v_product.revision);
    end if;

    v_validation := jsonb_build_object('validator', p_validator, 'result', p_result, 'preview', p_preview);
    if jsonb_array_length(p_result -> 'blocks') > 0 then
        update public.agent_products set validation = v_validation where id = v_product.id;
        perform public._agent_product_event(v_product, 'agent_product_validation_blocked',
            'automation_expert', p_requested_by_ref, 'agent_product_validation_blocked',
            jsonb_build_object('revision', v_product.revision,
                'codes', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                            from jsonb_array_elements(p_result -> 'blocks') f),
                'validator', p_validator));
        return jsonb_build_object('ok', true, 'status', 'borrador',
            'blocks', jsonb_array_length(p_result -> 'blocks'));
    end if;

    v_now := clock_timestamp();
    v_fingerprint := public._agent_product_fingerprint(v_product.id, v_product.revision,
        v_product.product_version, v_product.minimum, v_product.free_text, v_product.purpose);
    update public.agent_products
       set status = 'validada', fingerprint = v_fingerprint, validated_at = v_now,
           expires_at = v_now + interval '24 hours', validation = v_validation,
           warnings = p_result -> 'warnings'
     where id = v_product.id
    returning * into v_product;
    perform public._agent_product_event(v_product, 'agent_product_validated', 'automation_expert',
        p_requested_by_ref, 'agent_product_validated', jsonb_build_object('revision', v_product.revision,
            'fingerprint', v_fingerprint, 'validator', p_validator,
            'warnings', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                           from jsonb_array_elements(p_result -> 'warnings') f),
            'expires_at', v_product.expires_at));
    return jsonb_build_object('ok', true, 'status', 'validada', 'fingerprint', v_fingerprint,
        'expires_at', v_product.expires_at);
end;
$function$;

-- Obtener un producto, sin el ID de Slack de nadie.
create function public.get_agent_product_v1(p_instance jsonb, p_product_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_product public.agent_products%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_product from public.agent_products p
     where p.id = p_product_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_product_not_found';
    end if;
    return public._agent_product_view(v_product);
end;
$function$;

-- Los productos de la instancia, del mas nuevo al mas viejo, hasta p_limit (1 a
-- 50). Con p_only_open: los borradores, las validadas sin vencer, y las
-- publicadas o pausadas. Ademas, siempre y sin tope, la identidad de todos los
-- productos salvo los descartados (taken, product-card-v1 §2.1): lo que el
-- bridge mira para que un producto nuevo no tome la clave, el producto de
-- Hotmart, una oferta o un alias de otro.
create function public.list_agent_products_v1(p_instance jsonb, p_only_open boolean, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_rows jsonb;
    v_taken jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_only_open is null or p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'agent_product_list_parameters_invalid';
    end if;
    select coalesce(jsonb_agg(row_data order by created_at desc, product_id), '[]'::jsonb)
      into v_rows
      from (
        select p.created_at, p.id as product_id, jsonb_build_object(
                   'product_id', p.id,
                   'status', p.status,
                   'effective_status', case
                       when p.status = 'validada' and clock_timestamp() >= p.expires_at then 'vencida'
                       else p.status end,
                   'revision', p.revision,
                   'product_key', p.product_key,
                   'product_version', p.product_version,
                   'nombre', p.minimum ->> 'nombre',
                   'hotmart_product_id', p.hotmart_product_id,
                   'advertencias', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                                      from jsonb_array_elements(p.warnings) f),
                   'purpose', p.purpose,
                   'expires_at', p.expires_at,
                   'approved_at', p.approved_at,
                   'paused_at', p.paused_at,
                   'closed_at', p.closed_at,
                   'created_at', p.created_at) as row_data
          from public.agent_products p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and (not p_only_open
                or p.status in ('borrador', 'publicada', 'pausada')
                or (p.status = 'validada' and clock_timestamp() < p.expires_at))
         order by p.created_at desc, p.id
         limit p_limit) listed;
    select coalesce(jsonb_agg(jsonb_build_object(
               'product_id', p.id,
               'product_key', p.product_key,
               'status', p.status,
               'nombre', p.minimum ->> 'nombre',
               'alias', p.minimum -> 'alias',
               'hotmart_product_id', p.hotmart_product_id,
               'offer_codes', (select coalesce(jsonb_agg(o ->> 'codigo' order by o ->> 'codigo'), '[]'::jsonb)
                                 from jsonb_array_elements(p.minimum -> 'ofertas') o),
               'incluye_productos', p.minimum -> 'relaciones' -> 'incluye_productos')
             order by p.product_key, p.product_version), '[]'::jsonb)
      into v_taken
      from public.agent_products p
     where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
       and p.status <> 'descartada';
    return jsonb_build_object('products', v_rows, 'taken', v_taken,
        'open', public._agent_products_open(v_instance.o_tenant_ref, v_instance.o_ally_ref),
        'open_limit', 5, 'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 6. Los permisos. Las tablas, para nadie de la API. Las internas y los
--    disparadores, para nadie. Las cuatro publicas, solo para el AE.
-- ===========================================================================

do $acl$
declare
    v_table text;
    v_function regprocedure;
    v_role text;
begin
    foreach v_table in array array['agent_products', 'agent_product_events'] loop
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
        'public._agent_product_minimum_is_valid(jsonb)',
        'public._agent_product_findings_are_valid(jsonb)',
        'public.reject_agent_product_mutation()',
        'public.validate_agent_product_change()',
        'public._agent_product_event(public.agent_products,text,text,text,text,jsonb)',
        'public._agent_product_fingerprint(uuid,integer,integer,jsonb,text,text)',
        'public._agent_product_expire_if_due(public.agent_products)',
        'public._agent_product_view(public.agent_products)',
        'public._agent_products_open(text,text)',
        'public.save_agent_product_draft_v1(jsonb,uuid,integer,jsonb,text,jsonb,text,text)',
        'public.record_agent_product_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,text)',
        'public.get_agent_product_v1(jsonb,uuid)',
        'public.list_agent_products_v1(jsonb,boolean,integer)'
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
            'public.save_agent_product_draft_v1(jsonb,uuid,integer,jsonb,text,jsonb,text,text)',
            'public.record_agent_product_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,text)',
            'public.get_agent_product_v1(jsonb,uuid)',
            'public.list_agent_products_v1(jsonb,boolean,integer)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to automation_expert_change', v_function);
        end loop;
    end if;
end;
$acl$;

commit;
