-- hotmart-unknown-offers-v1: el registro de las ofertas huerfanas de Hotmart
-- (E-565, MP-02 del plan de multiproducto). Contrato:
-- docs/contracts/hotmart-unknown-offers-v1.md §4.
--
-- Por que. Un evento de Hotmart de un producto o una oferta que la instancia no
-- declara sale con 200 y, en el mejor caso, una linea de log. El 11/11 eso dejo
-- 14 de 24 compras del Black de ATT1 sin bienvenida (E-422) y nadie se entero
-- hasta mirar a mano. Esta tabla junta, sin datos personales, cada (producto,
-- oferta, tipo de evento) huerfano con su conteo, para que el aviso del OS
-- (MP-03) lo diga en el canal de la aliada.
--
-- Lo que trae: hotmart_unknown_offers (una fila por tenant, producto, oferta y
-- tipo de evento), hotmart_unknown_offer_refs (el hash del evento ya contado,
-- para que el reintento de Hotmart o del despachador no sume dos veces),
-- record_hotmart_unknown_offer_v1 (upsert idempotente) y
-- list_hotmart_unknown_offers_v1. Las dos RPC son solo de service_role.
--
-- Sin payload, sin mail, sin telefono, sin nombre, sin transaccion: el ID del
-- producto y el codigo de oferta son del catalogo de la aliada. El payload de 72 h
-- para el reproceso (D5) es otra tabla, de MP-17.
--
-- Lo que no hace: no toca webhook_events, recovery_cases ni scheduled_actions, y
-- no cambia ninguna respuesta del webhook. El bridge la llama solo con
-- HOTMART_UNKNOWN_OFFERS_ENABLED=true. Correrla dos veces aborta con 55000 sin
-- cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.hotmart_unknown_offers') is not null then
        raise exception using errcode = '55000', message = 'hotmart_unknown_offers_already_applied';
    end if;
end;
$already$;

create table public.hotmart_unknown_offers (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9_-]{0,79}$'),
    hotmart_product_id bigint not null check (hotmart_product_id > 0),
    offer_code text check (offer_code is null or offer_code ~ '^[A-Za-z0-9]{1,40}$'),
    event_type text not null check (event_type ~ '^[A-Z][A-Z_]{0,63}$'),
    motivo text not null check (motivo in ('product_not_declared', 'offer_not_declared')),
    entradas text[] not null
        check (cardinality(entradas) between 1 and 3
               and entradas <@ array['sin_ruta', 'parser_descarta', 'despacho']::text[]),
    -- Cero solo adentro de record_hotmart_unknown_offer_v1, entre el insert y el
    -- primer evento contado (la misma transaccion).
    conteo bigint not null default 0 check (conteo >= 0),
    primera_vez timestamptz not null default clock_timestamp(),
    ultima_vez timestamptz not null default clock_timestamp(),
    resuelta_en timestamptz,
    check (ultima_vez >= primera_vez)
);
create unique index hotmart_unknown_offers_key
    on public.hotmart_unknown_offers (tenant_ref, hotmart_product_id, (coalesce(offer_code, '')), event_type);
create index hotmart_unknown_offers_open_idx
    on public.hotmart_unknown_offers (tenant_ref, ultima_vez desc) where resuelta_en is null;

create table public.hotmart_unknown_offer_refs (
    offer_id uuid not null references public.hotmart_unknown_offers(id) on delete cascade,
    event_ref_sha256 text not null check (event_ref_sha256 ~ '^[0-9a-f]{64}$'),
    entrada text not null check (entrada in ('sin_ruta', 'parser_descarta', 'despacho')),
    created_at timestamptz not null default clock_timestamp(),
    primary key (offer_id, event_ref_sha256)
);

do $tables_acl$
declare
    v_table text;
    v_role text;
begin
    foreach v_table in array array['hotmart_unknown_offers', 'hotmart_unknown_offer_refs'] loop
        execute format('alter table public.%I enable row level security', v_table);
        execute format('revoke all on table public.%I from public', v_table);
        for v_role in select r.rolname from pg_roles r
                       where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname loop
            execute format('revoke all on table public.%I from %I', v_table, v_role);
        end loop;
    end loop;
end;
$tables_acl$;

-- El registro: una fila por (tenant, producto, oferta, tipo de evento). El mismo
-- evento (p_event_ref_sha256) repetido no suma: devuelve duplicate. Un evento
-- nuevo de una fila resuelta la vuelve a abrir (sigue llegando, sigue huerfana).
-- Desde el despachador (entrada despacho) el bridge no sabe el tipo del evento:
-- lo lee de webhook_events por p_source_event_id (el tipo y el origen). Un caso
-- abierto por un formulario o una audiencia (origen distinto de hotmart) no deja
-- fila y devuelve not_hotmart.
create function public.record_hotmart_unknown_offer_v1(
    p_tenant_ref text,
    p_hotmart_product_id bigint,
    p_offer_code text,
    p_event_type text,
    p_motivo text,
    p_entrada text,
    p_event_ref_sha256 text,
    p_source_event_id uuid default null
)
returns table (offer_id uuid, outcome text, conteo bigint)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_event_type text := p_event_type;
    v_source text;
    v_offer_id uuid;
    v_counted integer;
    v_conteo bigint;
    v_was_new boolean;
begin
    if p_tenant_ref is null or p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,79}$'
       or p_hotmart_product_id is null or p_hotmart_product_id <= 0
       or (p_offer_code is not null and p_offer_code !~ '^[A-Za-z0-9]{1,40}$')
       or p_motivo is null or p_motivo not in ('product_not_declared', 'offer_not_declared')
       or p_entrada is null or p_entrada not in ('sin_ruta', 'parser_descarta', 'despacho')
       or p_event_ref_sha256 is null or p_event_ref_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception using errcode = '22023', message = 'hotmart_unknown_offer_invalid_arguments';
    end if;
    if v_event_type is null and p_source_event_id is not null then
        select event.event_type, event.source into v_event_type, v_source
          from public.webhook_events event
         where event.id = p_source_event_id;
        if v_source is not null and v_source <> 'hotmart' then
            -- El caso lo abrio un formulario o una audiencia, no Hotmart: no es
            -- una oferta huerfana de Hotmart y no deja fila.
            return query select null::uuid, 'not_hotmart'::text, 0::bigint;
            return;
        end if;
    end if;
    if v_event_type is null or v_event_type !~ '^[A-Z][A-Z_]{0,63}$' then
        raise exception using errcode = '22023', message = 'hotmart_unknown_offer_invalid_event_type';
    end if;

    insert into public.hotmart_unknown_offers as offer
        (tenant_ref, hotmart_product_id, offer_code, event_type, motivo, entradas)
    values (p_tenant_ref, p_hotmart_product_id, p_offer_code, v_event_type, p_motivo, array[p_entrada])
    on conflict (tenant_ref, hotmart_product_id, (coalesce(offer_code, '')), event_type) do nothing;

    select offer.id, offer.conteo = 0 into v_offer_id, v_was_new
      from public.hotmart_unknown_offers offer
     where offer.tenant_ref = p_tenant_ref
       and offer.hotmart_product_id = p_hotmart_product_id
       and coalesce(offer.offer_code, '') = coalesce(p_offer_code, '')
       and offer.event_type = v_event_type
       for update;

    insert into public.hotmart_unknown_offer_refs (offer_id, event_ref_sha256, entrada)
    values (v_offer_id, p_event_ref_sha256, p_entrada)
    on conflict on constraint hotmart_unknown_offer_refs_pkey do nothing;
    get diagnostics v_counted = row_count;

    if v_counted = 0 then
        select offer.conteo into v_conteo from public.hotmart_unknown_offers offer where offer.id = v_offer_id;
        return query select v_offer_id, 'duplicate'::text, v_conteo;
        return;
    end if;

    update public.hotmart_unknown_offers offer
       set conteo = offer.conteo + 1,
           ultima_vez = greatest(offer.ultima_vez, clock_timestamp()),
           motivo = p_motivo,
           entradas = case when p_entrada = any(offer.entradas) then offer.entradas
                           else offer.entradas || p_entrada end,
           resuelta_en = null
     where offer.id = v_offer_id
     returning offer.conteo into v_conteo;

    return query select v_offer_id, case when v_was_new then 'inserted' else 'counted' end, v_conteo;
end;
$function$;

-- Las filas, para el aviso del OS (MP-03), /ready y recuperador_estado.py. Por
-- defecto, solo las abiertas; la mas reciente primero.
create function public.list_hotmart_unknown_offers_v1(
    p_tenant_ref text,
    p_solo_abiertas boolean default true
)
returns table (
    offer_id uuid,
    hotmart_product_id bigint,
    offer_code text,
    event_type text,
    motivo text,
    entradas text[],
    conteo bigint,
    primera_vez timestamptz,
    ultima_vez timestamptz,
    resuelta_en timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_tenant_ref is null or p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,79}$' then
        raise exception using errcode = '22023', message = 'hotmart_unknown_offer_invalid_arguments';
    end if;
    return query
    select offer.id, offer.hotmart_product_id, offer.offer_code, offer.event_type, offer.motivo,
           offer.entradas, offer.conteo, offer.primera_vez, offer.ultima_vez, offer.resuelta_en
      from public.hotmart_unknown_offers offer
     where offer.tenant_ref = p_tenant_ref
       and offer.conteo > 0
       and (not coalesce(p_solo_abiertas, true) or offer.resuelta_en is null)
     order by offer.ultima_vez desc, offer.id
     limit 500;
end;
$function$;

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public.record_hotmart_unknown_offer_v1(text,bigint,text,text,text,text,text,uuid)',
        'public.list_hotmart_unknown_offers_v1(text,boolean)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in select r.rolname from pg_roles r
                       where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.record_hotmart_unknown_offer_v1(text,bigint,text,text,text,text,text,uuid) to service_role;
        grant execute on function public.list_hotmart_unknown_offers_v1(text,boolean) to service_role;
    end if;
end;
$acl$;

commit;
