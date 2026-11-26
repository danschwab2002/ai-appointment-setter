-- contact-export-v1: exportar contactos y listas (funcion 1.3 de la lista
-- definitiva del 2026-11-20; pieza 4 del mapa de las funciones faltantes de Z1
-- del 2026-11-26, decisiones D7 y D12). Contrato: docs/contracts/contact-export-v1.md.
--
-- El Automation Expert arma un pedido de exportacion con filtros; al hilo de
-- Slack vuelven solo el conteo y el link. En la pagina, un aprobador con la
-- sesion de «Entrar con Slack» (20261028000100) baja el CSV una vez, dentro de
-- los 30 minutos del pedido. Nadie la usa todavia: sin CONTACT_EXPORT_ENABLED
-- el bridge no llama a nada de esto.
--
-- 1. Una tabla nueva, vacia:
--    * contact_exports: el pedido. Los filtros, las columnas, cuantas personas
--      coincidian al pedirlo, quien lo pidio (slack:<id>), cuando vence (30
--      minutos despues de pedirlo) y, una vez bajado, quien lo bajo, cuando y
--      cuantas filas. Ningun telefono, mail ni nombre: el CSV se arma de nuevo al
--      bajarlo y no se guarda.
-- 2. Una funcion interna, de nadie:
--    * _contact_export_people: las personas de la base de la instancia, una por
--      telefono canonico (_whatsapp_phone_canonical), en orden de telefono y con
--      cursor: el nombre y el mail (del contacto, del formulario mas nuevo o del
--      comprador mas nuevo), los formularios del tenant (los 20 mas nuevos, con
--      su producto), los productos comprados (una compra aprobada sin reembolso
--      ni contracargo, atada al tenant: la misma lectura de compras que los
--      vetos del masivo de 20261125000300) y la baja (la regla de esos mismos
--      vetos: un evento de baja o una autorizacion denegada). Supone una base por
--      instancia: contacts no lleva tenant_ref.
-- 3. Funciones del bridge, solo de service_role:
--    * create_contact_export_v1: el pedido que arma el AE. Hasta 20 por
--      instancia por hora.
--    * read_contact_export_v1: el pedido y su estado (pendiente, bajado, vencido),
--      sin quien lo pidio ni quien lo bajo.
--    * read_contact_export_people_v1: _contact_export_people, para el conteo.
-- 4. Funciones de la pagina, solo del rol flow_change_approver, con la sesion:
--    * approver_read_contact_export_v1: el pedido, para la pagina.
--    * approver_read_contact_export_people_v1: las personas, solo con el pedido
--      pendiente (sin bajar y sin vencer).
--    * approver_mark_contact_export_downloaded_v1: lo marca bajado, una vez y
--      antes de que venza.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente. Correrla dos
-- veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida, P0002 lo que no existe, 28000 la sesion. Los
-- resultados de estado vuelven como {"ok": false, "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regprocedure('public._whatsapp_phone_canonical(text)') is null
       or to_regrole('flow_change_approver') is null
       or to_regclass('public.contacts') is null
       or to_regclass('public.contact_points') is null
       or to_regclass('public.channel_identities') is null
       or to_regclass('public.purchase_intents') is null
       or to_regclass('public.purchase_intent_submissions') is null
       or to_regclass('public.precheckout_submissions') is null
       or to_regclass('public.contact_opt_out_events') is null
       or to_regclass('public.contact_authorizations') is null
       or to_regclass('public.portable_hotmart_purchase_correlations') is null then
        raise exception using errcode = '55000', message = 'contact_export_prerequisite_missing';
    end if;
    if to_regclass('public.contact_exports') is not null then
        raise exception using errcode = '55000', message = 'contact_export_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. La tabla.
-- ===========================================================================

create table public.contact_exports (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null,
    ally_ref text not null,
    filters jsonb not null check (jsonb_typeof(filters) = 'object' and octet_length(filters::text) <= 4096),
    columns text[] not null check (
        cardinality(columns) between 1 and 9 and array_position(columns, null) is null
        and columns <@ array['nombre', 'telefono', 'email', 'etiquetas', 'compro', 'ultimo_formulario', 'pais', 'baja',
                             'productos']::text[]),
    matched_count integer not null check (matched_count between 0 and 20000),
    requested_by text not null check (requested_by ~ '^slack:[UW][A-Z0-9]{8,20}$'),
    created_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz not null,
    downloaded_at timestamptz,
    downloaded_by text check (downloaded_by is null or downloaded_by ~ '^slack:[UW][A-Z0-9]{8,20}$'),
    downloaded_count integer check (downloaded_count is null or downloaded_count between 0 and 20000),
    check (expires_at > created_at and expires_at <= created_at + interval '30 minutes'),
    check ((downloaded_at is null) = (downloaded_by is null) and (downloaded_at is null) = (downloaded_count is null)),
    check (downloaded_at is null or downloaded_at <= expires_at)
);
create index contact_exports_instance_created
    on public.contact_exports (tenant_ref, ally_ref, created_at desc);

alter table public.contact_exports enable row level security;

-- ===========================================================================
-- 2. Las personas.
-- ===========================================================================

-- {"people": [{"phone", "name", "email", "forms": [{"at", "product"}], "purchased_products", "opted_out"}],
--  "next_after": <el ultimo telefono de la pagina, o null si no hay mas>}
-- Las fuentes se leen una vez por pagina y se cruzan por el telefono canonico (hash join, sin una subconsulta por
-- persona). p_after es el ultimo telefono de la pagina anterior.
create function public._contact_export_people(p_tenant_ref text, p_after text, p_limit integer)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    with
    contact_phones as materialized (
        select c.id as contact_id, public._whatsapp_phone_canonical(c.phone) as phone
          from public.contacts c where c.phone is not null
        union
        select p.contact_id, public._whatsapp_phone_canonical(p.normalized_value)
          from public.contact_points p where p.type = 'phone'
        union
        select i.contact_id, public._whatsapp_phone_canonical(i.external_user_id)
          from public.channel_identities i where i.channel = 'whatsapp' and i.external_user_id is not null
    ),
    forms as materialized (
        select public._whatsapp_phone_canonical(intent.normalized_phone) as phone, intent.id as intent_id,
               intent.funnel_ref, intent.submitted_at, nullif(lower(btrim(intent.normalized_email)), '') as email
          from public.purchase_intents intent
         where intent.tenant_ref = p_tenant_ref
    ),
    purchases as materialized (
        select public._whatsapp_phone_canonical(coalesce(event.payload #>> '{data,buyer,checkout_phone}',
                                                         event.payload #>> '{data,buyer,phone}')) as phone,
               correlation.funnel_ref, event.received_at as at,
               nullif(btrim(event.payload #>> '{data,buyer,name}'), '') as full_name,
               nullif(lower(btrim(event.payload #>> '{data,buyer,email}')), '') as email
          from public.webhook_events event
          join public.portable_hotmart_purchase_correlations correlation on correlation.webhook_event_id = event.id
         where event.source = 'hotmart' and event.event_type = 'PURCHASE_APPROVED'
           and correlation.tenant_ref = p_tenant_ref
           and not exists (
               select 1 from public.webhook_events reversal
                where reversal.source = 'hotmart'
                  and reversal.event_type in ('PURCHASE_REFUNDED', 'PURCHASE_CHARGEBACK', 'PURCHASE_PROTEST')
                  and nullif(event.payload #>> '{data,purchase,transaction}', '') is not null
                  and reversal.payload #>> '{data,purchase,transaction}' = event.payload #>> '{data,purchase,transaction}')
    ),
    universe as (
        select phone from contact_phones
        union select phone from forms
        union select phone from purchases
    ),
    page as materialized (
        select u.phone from universe u
         where u.phone ~ '^[1-9][0-9]{7,14}$' and (p_after is null or u.phone > p_after)
         order by u.phone
         limit p_limit
    ),
    contact_side as (
        select page.phone,
               (array_agg(btrim(c.full_name) order by c.updated_at desc, c.id)
                    filter (where nullif(btrim(c.full_name), '') is not null))[1] as full_name,
               (array_agg(lower(btrim(c.email)) order by c.updated_at desc, c.id)
                    filter (where nullif(btrim(c.email), '') is not null))[1] as email
          from page
          join contact_phones cp on cp.phone = page.phone
          join public.contacts c on c.id = cp.contact_id
         group by page.phone
    ),
    form_side as (
        select page.phone,
               (array_agg(f.intent_id order by f.submitted_at desc, f.intent_id))[1] as last_intent_id,
               (array_agg(f.email order by f.submitted_at desc, f.intent_id) filter (where f.email is not null))[1]
                   as email,
               (array_agg(jsonb_build_object('at', f.submitted_at, 'product', f.funnel_ref)
                          order by f.submitted_at desc, f.intent_id))[1:20] as forms
          from page join forms f on f.phone = page.phone
         group by page.phone
    ),
    form_name as (
        select fs.phone,
               (select nullif(btrim(s.canonical_payload #>> '{lead,full_name}'), '')
                  from public.purchase_intent_submissions link
                  join public.precheckout_submissions s on s.id = link.submission_id
                 where link.purchase_intent_id = fs.last_intent_id
                 order by link.ordinal desc limit 1) as full_name
          from form_side fs
    ),
    purchase_side as (
        select page.phone,
               array_agg(distinct p.funnel_ref) as products,
               (array_agg(p.full_name order by p.at desc) filter (where p.full_name is not null))[1] as full_name,
               (array_agg(p.email order by p.at desc) filter (where p.email is not null))[1] as email
          from page join purchases p on p.phone = page.phone
         group by page.phone
    ),
    opted as (
        select distinct page.phone
          from page
          join (
              select public._whatsapp_phone_canonical(o.external_user_id) as phone
                from public.contact_opt_out_events o where o.channel = 'whatsapp'
              union
              select cp.phone
                from public.contact_authorizations ca
                join contact_phones cp on cp.contact_id = ca.contact_id
               where ca.channel = 'whatsapp' and ca.authorization_status = 'denied'
                 and ca.valid_from <= clock_timestamp()
                 and (ca.valid_until is null or ca.valid_until > clock_timestamp())
          ) baja on baja.phone = page.phone
    )
    select jsonb_build_object(
        'people', coalesce(jsonb_agg(jsonb_build_object(
            'phone', page.phone,
            'name', coalesce(cs.full_name, fn.full_name, ps.full_name),
            'email', coalesce(cs.email, fs.email, ps.email),
            'forms', coalesce(to_jsonb(fs.forms), '[]'::jsonb),
            'purchased_products', coalesce(to_jsonb(ps.products), '[]'::jsonb),
            'opted_out', op.phone is not null
        ) order by page.phone), '[]'::jsonb),
        'next_after', case when count(*) = p_limit then max(page.phone) end)
      from page
      left join contact_side cs on cs.phone = page.phone
      left join form_side fs on fs.phone = page.phone
      left join form_name fn on fn.phone = page.phone
      left join purchase_side ps on ps.phone = page.phone
      left join opted op on op.phone = page.phone
$function$;

-- ===========================================================================
-- 3. Las funciones del bridge (service_role).
-- ===========================================================================

create function public._contact_export_check_page(p_after text, p_limit integer)
returns void
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_limit is null or p_limit < 1 or p_limit > 1000
       or (p_after is not null and p_after !~ '^[1-9][0-9]{7,14}$') then
        raise exception using errcode = '22023', message = 'contact_export_page_invalid';
    end if;
end;
$function$;

-- La vista de un pedido. p_with_people: si suma quien lo pidio y quien lo bajo (solo la pagina).
create function public._contact_export_view(p_export public.contact_exports, p_with_people boolean)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'export_id', p_export.id,
        'state', case when p_export.downloaded_at is not null then 'bajado'
                      when p_export.expires_at <= clock_timestamp() then 'vencido'
                      else 'pendiente' end,
        'filters', p_export.filters,
        'columns', to_jsonb(p_export.columns),
        'matched_count', p_export.matched_count,
        'created_at', p_export.created_at,
        'expires_at', p_export.expires_at,
        'downloaded_at', p_export.downloaded_at,
        'downloaded_count', p_export.downloaded_count)
        || case when p_with_people then jsonb_build_object(
               'requested_by', p_export.requested_by, 'downloaded_by', p_export.downloaded_by)
           else '{}'::jsonb end
$function$;

-- p_export = {"filters": {...}, "columns": [...], "matched_count": n, "requested_by": "U..."}.
create function public.create_contact_export_v1(p_instance jsonb, p_export jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_columns text[];
    v_id uuid;
    v_expires timestamptz;
begin
    v_instance := public._flow_store_instance(p_instance);
    if jsonb_typeof(p_export) is distinct from 'object'
       or not (p_export ?& array['filters', 'columns', 'matched_count', 'requested_by'])
       or exists (select 1 from jsonb_object_keys(p_export) k
                   where k not in ('filters', 'columns', 'matched_count', 'requested_by'))
       or jsonb_typeof(p_export -> 'filters') is distinct from 'object'
       or jsonb_typeof(p_export -> 'columns') is distinct from 'array'
       or jsonb_typeof(p_export -> 'matched_count') is distinct from 'number'
       or not coalesce(p_export ->> 'requested_by' ~ '^[UW][A-Z0-9]{8,20}$', false)
       or exists (select 1 from jsonb_array_elements(p_export -> 'columns') e where jsonb_typeof(e) <> 'string') then
        raise exception using errcode = '22023', message = 'contact_export_invalid';
    end if;
    if (p_export ->> 'matched_count') !~ '^[0-9]{1,5}$' or (p_export ->> 'matched_count')::integer > 20000 then
        raise exception using errcode = '22023', message = 'contact_export_invalid';
    end if;
    select array_agg(e order by o) into v_columns
      from jsonb_array_elements_text(p_export -> 'columns') with ordinality as c(e, o);
    if v_columns is null or cardinality(v_columns) > 9
       or cardinality(v_columns) <> (select count(distinct x) from unnest(v_columns) x)
       or not (v_columns <@ array['nombre', 'telefono', 'email', 'etiquetas', 'compro', 'ultimo_formulario', 'pais',
                                  'baja', 'productos']::text[])
       or octet_length((p_export -> 'filters')::text) > 4096 then
        raise exception using errcode = '22023', message = 'contact_export_invalid';
    end if;
    -- Hasta 20 pedidos por instancia por hora: el link lleva datos personales y no se arma en serie.
    perform pg_advisory_xact_lock(hashtext('contact_export:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref));
    if (select count(*) from public.contact_exports e
         where e.tenant_ref = v_instance.o_tenant_ref and e.ally_ref = v_instance.o_ally_ref
           and e.created_at > clock_timestamp() - interval '1 hour') >= 20 then
        return jsonb_build_object('ok', false, 'reason_code', 'contact_export_rate_limited');
    end if;
    insert into public.contact_exports (tenant_ref, ally_ref, filters, columns, matched_count, requested_by,
                                        created_at, expires_at)
    values (v_instance.o_tenant_ref, v_instance.o_ally_ref, p_export -> 'filters', v_columns,
            (p_export ->> 'matched_count')::integer, 'slack:' || (p_export ->> 'requested_by'),
            clock_timestamp(), clock_timestamp() + interval '30 minutes')
    returning id, expires_at into v_id, v_expires;
    return jsonb_build_object('ok', true, 'export_id', v_id, 'expires_at', v_expires);
end;
$function$;

create function public.read_contact_export_v1(p_instance jsonb, p_export_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_export public.contact_exports;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_export from public.contact_exports e
     where e.id = p_export_id and e.tenant_ref = v_instance.o_tenant_ref and e.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'contact_export_not_found';
    end if;
    return public._contact_export_view(v_export, false);
end;
$function$;

create function public.read_contact_export_people_v1(p_instance jsonb, p_after text, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._contact_export_check_page(p_after, p_limit);
    return public._contact_export_people(v_instance.o_tenant_ref, p_after, p_limit);
end;
$function$;

-- ===========================================================================
-- 4. Las funciones de la pagina (flow_change_approver, con la sesion).
-- ===========================================================================

create function public.approver_read_contact_export_v1(p_instance jsonb, p_session_hash text, p_export_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_export public.contact_exports;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_export from public.contact_exports e
     where e.id = p_export_id and e.tenant_ref = v_instance.o_tenant_ref and e.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'contact_export_not_found';
    end if;
    return public._contact_export_view(v_export, true);
end;
$function$;

create function public.approver_read_contact_export_people_v1(
    p_instance jsonb, p_session_hash text, p_export_id uuid, p_after text, p_limit integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_export public.contact_exports;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    perform public._contact_export_check_page(p_after, p_limit);
    select * into v_export from public.contact_exports e
     where e.id = p_export_id and e.tenant_ref = v_instance.o_tenant_ref and e.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'contact_export_not_found';
    end if;
    if v_export.downloaded_at is not null then
        return jsonb_build_object('ok', false, 'reason_code', 'contact_export_already_downloaded');
    end if;
    if v_export.expires_at <= clock_timestamp() then
        return jsonb_build_object('ok', false, 'reason_code', 'contact_export_expired');
    end if;
    return jsonb_build_object('ok', true) || public._contact_export_people(v_instance.o_tenant_ref, p_after, p_limit);
end;
$function$;

create function public.approver_mark_contact_export_downloaded_v1(
    p_instance jsonb, p_session_hash text, p_export_id uuid, p_count integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_actor text;
    v_export public.contact_exports;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if p_count is null or p_count < 0 or p_count > 20000 then
        raise exception using errcode = '22023', message = 'contact_export_count_invalid';
    end if;
    select * into v_export from public.contact_exports e
     where e.id = p_export_id and e.tenant_ref = v_instance.o_tenant_ref and e.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'contact_export_not_found';
    end if;
    if v_export.downloaded_at is not null then
        return jsonb_build_object('ok', false, 'reason_code', 'contact_export_already_downloaded');
    end if;
    if v_export.expires_at <= clock_timestamp() then
        return jsonb_build_object('ok', false, 'reason_code', 'contact_export_expired');
    end if;
    update public.contact_exports
       set downloaded_at = clock_timestamp(), downloaded_by = 'slack:' || v_actor, downloaded_count = p_count
     where id = p_export_id;
    return jsonb_build_object('ok', true);
end;
$function$;

-- ===========================================================================
-- 5. Los permisos. La tabla, de nadie; las del bridge, solo service_role; las de
--    la pagina, solo flow_change_approver; las internas, de nadie.
-- ===========================================================================

do $roles$
declare
    v_function regprocedure;
    v_role text;
begin
    execute 'revoke all on table public.contact_exports from public';
    for v_role in
        select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by 1
    loop
        execute format('revoke all on table public.contact_exports from %I', v_role);
    end loop;
    foreach v_function in array array[
        'public._contact_export_people(text,text,integer)',
        'public._contact_export_check_page(text,integer)',
        'public._contact_export_view(public.contact_exports,boolean)',
        'public.create_contact_export_v1(jsonb,jsonb)',
        'public.read_contact_export_v1(jsonb,uuid)',
        'public.read_contact_export_people_v1(jsonb,text,integer)',
        'public.approver_read_contact_export_v1(jsonb,text,uuid)',
        'public.approver_read_contact_export_people_v1(jsonb,text,uuid,text,integer)',
        'public.approver_mark_contact_export_downloaded_v1(jsonb,text,uuid,integer)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role', 'flow_change_approver') order by 1
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.create_contact_export_v1(jsonb, jsonb) to service_role;
        grant execute on function public.read_contact_export_v1(jsonb, uuid) to service_role;
        grant execute on function public.read_contact_export_people_v1(jsonb, text, integer) to service_role;
    end if;
    grant execute on function public.approver_read_contact_export_v1(jsonb, text, uuid) to flow_change_approver;
    grant execute on function public.approver_read_contact_export_people_v1(jsonb, text, uuid, text, integer)
        to flow_change_approver;
    grant execute on function public.approver_mark_contact_export_downloaded_v1(jsonb, text, uuid, integer)
        to flow_change_approver;
end;
$roles$;

commit;
