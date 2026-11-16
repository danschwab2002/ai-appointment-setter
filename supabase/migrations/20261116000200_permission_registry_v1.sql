-- permission-registry-v1: el registro de permiso de WhatsApp y la segunda rama
-- del permiso del carrito y del pago rechazado nativos (E-627, M1 de E-624,
-- 2026-11-16). Contrato: docs/contracts/permission-registry-v1.md.
--
-- Por que. Hoy el permiso del carrito y del pago rechazado no es un registro: es
-- una purchase_intent de un formulario, con whatsapp_contact_authorized, que la
-- correlacion ato a ESE evento de Hotmart. Sin formulario no hay intencion y el
-- resultado es no_entra:permiso. El combo de Maribel (19/11) no tiene formulario:
-- el permiso sale de un aviso en el checkout o en la landing, que un conector
-- (M2) convierte en una fila de permiso. El nucleo solo pregunta si hay un
-- permiso registrado; de donde salio lo resuelve el conector (opcion A, aprobada
-- en su enfoque por Dan el 16/11).
--
-- Que trae:
--   1. permission_notices: el aviso declarado (lo que una fuente afirma mostrar,
--      medido en la pagina publicada), por instancia, producto y ofertas. Solo
--      se agrega; lo unico que cambia despues es valid_until (el cierre).
--   2. permission_grants: el permiso de una persona (telefono canonico, producto
--      y oferta, usos, ventana), respaldado por un aviso vigente al otorgarse.
--      Solo se agrega; lo unico que cambia despues es la revocacion y el
--      contacto (de nulo a un valor). unique (tenant_ref, connector, evidence_ref):
--      un evento repetido no duplica el permiso.
--   3. Cuatro RPC de service_role: register_permission_notice_v1,
--      close_permission_notice_v1, record_permission_grant_v1 (valida que el
--      aviso exista, sea del conector, este vigente al otorgarse, cubra la
--      oferta y los usos) y revoke_permission_grant_v1.
--   4. _registered_permission_reason(tenant, contacto, destino, producto,
--      oferta, momento, uso): la unica pregunta del nucleo. null si hay un
--      permiso vigente; si no, baja o no_entra:permiso, los motivos de hoy.
--   5. _flow_engine_native_registered_audience: el envoltorio del carrito y del
--      pago rechazado (el evento es de la instancia, del producto y de la oferta;
--      un cancelado que no es un rechazo da no_es_un_rechazo) y
--      _registered_permission_recheck, lo que mira la puerta antes de cada envio.
--   6. flow_engine_native_runs.audience_permission_grant_id y la restriccion
--      audience_by_trigger: el carrito o el pago rechazado pueden nacer sin
--      intencion solo con un permiso registrado.
--   7. _flow_engine_admit_native_flow: con el carrito y el pago rechazado, la
--      rama del formulario va PRIMERO e intacta (y en el pago rechazado, la del
--      telefono de la campana despues). Solo si termina en no_entra:permiso se
--      consulta el registro. La autorizacion queda con la evidencia
--      registered_permission y el id del permiso.
--   8. _flow_engine_authorize_native_tail: un recorrido con permiso registrado
--      no tiene intencion que mirar otra vez; la puerta mira el permiso
--      (revocado: ya_no_entra) y la baja (la del contacto, la de Chatwoot de ese
--      movil y la registrada) antes de cada envio.
--
-- Lo que NO cambia. Sin filas en permission_grants (hoy en todas las instancias:
-- nada las escribe hasta M2 con su flag prendido) la admision es exactamente la
-- de antes: lo que era no_entra:permiso sigue siendolo. Un formulario que da
-- permiso no llega nunca a la rama nueva. La ficha, la intencion, la compra y la
-- audiencia no consultan el registro. Las filas existentes del recorrido quedan
-- con audience_permission_grant_id nulo y cumplen la restriccion nueva.
--
-- Como: el metodo de 20261112210000, reemplazos contados sobre la definicion
-- VIGENTE, con la marca que no puede estar. Correrla dos veces aborta con 55000.
--
-- Errores: 22023 argumentos invalidos; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $guard$
begin
    if to_regclass('public.permission_grants') is not null
       or to_regclass('public.permission_notices') is not null then
        raise exception using errcode = '55000', message = 'permission_registry_already_applied';
    end if;
    if to_regprocedure('public._flow_engine_native_cart_audience(uuid,uuid,text,text,text)') is null
       or to_regprocedure('public._flow_engine_native_payment_failure_phone_audience(uuid,uuid,text,text,text)') is null
       or to_regprocedure('public._portable_chatwoot_opt_out_stop(bigint,uuid,text)') is null then
        raise exception using errcode = '55000', message = 'permission_registry_missing';
    end if;
end;
$guard$;

-- Los reemplazos por ancla, con la misma verificacion para todos.
create function pg_temp.permission_registry_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
    v_acl text;
    v_secdef boolean;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'permission_registry_function_missing',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_permission_registry_definition',
            detail = p_signature;
    end if;
    select coalesce(p.proacl::text, ''), p.prosecdef into v_acl, v_secdef
      from pg_proc p where p.oid = p_signature::regprocedure;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'permission_registry_not_replaced',
            detail = p_signature;
    end if;
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = p_signature::regprocedure) is distinct from v_acl
       or (select p.prosecdef from pg_proc p where p.oid = p_signature::regprocedure) is distinct from v_secdef then
        raise exception using errcode = '55000', message = 'permission_registry_acl_changed',
            detail = p_signature;
    end if;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 1. El aviso declarado.
-- ---------------------------------------------------------------------------
create table public.permission_notices (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9_-]{0,79}$'),
    notice_ref text not null check (notice_ref ~ '^[a-z0-9][a-z0-9._-]{0,119}$'),
    -- Texto libre para el nucleo: lo usa solo el conector (hotmart_checkout,
    -- landing, ghl_form, lead_precheckout).
    connector text not null check (connector ~ '^[a-z][a-z0-9_]{0,39}$'),
    -- El producto de la instancia (flow_controls.product_ref, el funnel del
    -- binding) y las ofertas; sin ofertas, todo el producto.
    product_ref text not null check (product_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$'),
    offer_refs text[] not null default array[]::text[]
        check (cardinality(offer_refs) <= 50
               and array_position(offer_refs, null) is null),
    uses text[] not null
        check (cardinality(uses) between 1 and 2
               and uses <@ array['recuperacion', 'difusion']::text[]),
    surface_url text not null
        check (surface_url ~ '^https://[^[:space:]]+$' and char_length(surface_url) <= 2048),
    copy_version text not null check (copy_version ~ '^[a-z0-9][a-z0-9._-]{0,119}$'),
    notice_text text not null check (char_length(notice_text) between 1 and 4000),
    notice_text_sha256 text not null check (notice_text_sha256 ~ '^[0-9a-f]{64}$'),
    checkbox text not null check (checkbox in ('ninguna', 'opcional_sin_premarcar', 'obligatoria')),
    measured_at timestamptz not null,
    measured_by text not null check (char_length(btrim(measured_by)) between 1 and 200),
    valid_from timestamptz not null,
    valid_until timestamptz,
    -- Solo landing: el origin.src que tiene que traer un pago rechazado (lo mira el conector).
    requires_src text check (requires_src is null or requires_src ~ '^[A-Za-z0-9_.:-]{1,255}$'),
    created_at timestamptz not null default clock_timestamp(),
    unique (tenant_ref, notice_ref),
    check (notice_text_sha256 = encode(sha256(convert_to(notice_text, 'UTF8')), 'hex')),
    -- El aviso cuenta desde que se midio publicado, nunca antes.
    check (valid_from >= measured_at),
    check (valid_until is null or valid_until >= valid_from)
);
create index permission_notices_scope_idx
    on public.permission_notices (tenant_ref, product_ref, connector);

-- ---------------------------------------------------------------------------
-- 2. El permiso de una persona.
-- ---------------------------------------------------------------------------
create table public.permission_grants (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9_-]{0,79}$'),
    -- El telefono canonico (_whatsapp_phone_canonical): 52 sin el 1, 54 sin el 9.
    normalized_phone text not null check (normalized_phone ~ '^[0-9]{8,15}$'),
    contact_id uuid references public.contacts(id) on delete restrict,
    normalized_email text check (normalized_email is null or char_length(normalized_email) between 3 and 320),
    channel text not null default 'whatsapp' check (channel = 'whatsapp'),
    product_ref text not null check (product_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$'),
    -- Nulo: todo el producto.
    offer_ref text check (offer_ref is null or offer_ref ~ '^[A-Za-z0-9_-]{1,64}$'),
    uses text[] not null
        check (cardinality(uses) between 1 and 2
               and uses <@ array['recuperacion', 'difusion']::text[]),
    connector text not null check (connector ~ '^[a-z][a-z0-9_]{0,39}$'),
    connector_version text not null check (connector_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$'),
    evidence_kind text not null check (evidence_kind in ('envio_persona', 'aviso_en_el_paso')),
    -- El precheckout_submission_id o el webhook_event_id del acto.
    evidence_ref uuid not null,
    notice_ref text not null,
    copy_version text not null,
    granted_at timestamptz not null,
    expires_at timestamptz not null,
    revoked_at timestamptz,
    revoked_by text check (revoked_by is null or char_length(btrim(revoked_by)) between 1 and 200),
    created_at timestamptz not null default clock_timestamp(),
    unique (tenant_ref, connector, evidence_ref),
    foreign key (tenant_ref, notice_ref) references public.permission_notices (tenant_ref, notice_ref)
        on delete restrict,
    check (expires_at > granted_at),
    check ((revoked_at is null) = (revoked_by is null))
);
create index permission_grants_lookup_idx
    on public.permission_grants (tenant_ref, normalized_phone, product_ref, granted_at desc);

-- Solo se agrega: el aviso solo se cierra; el permiso solo se revoca o gana su
-- contacto. Nada se borra.
create function public._permission_registry_append_only()
returns trigger
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000', message = 'permission_registry_append_only',
            detail = tg_table_name;
    end if;
    if tg_table_name = 'permission_notices' then
        if (to_jsonb(new) - 'valid_until') is distinct from (to_jsonb(old) - 'valid_until')
           or (old.valid_until is not null and new.valid_until is distinct from old.valid_until) then
            raise exception using errcode = '55000', message = 'permission_registry_append_only',
                detail = tg_table_name;
        end if;
    else
        if (to_jsonb(new) - array['revoked_at', 'revoked_by', 'contact_id'])
               is distinct from (to_jsonb(old) - array['revoked_at', 'revoked_by', 'contact_id'])
           or (old.revoked_at is not null
               and (new.revoked_at is distinct from old.revoked_at or new.revoked_by is distinct from old.revoked_by))
           or (old.contact_id is not null and new.contact_id is distinct from old.contact_id) then
            raise exception using errcode = '55000', message = 'permission_registry_append_only',
                detail = tg_table_name;
        end if;
    end if;
    return new;
end;
$function$;
create trigger permission_notices_append_only
    before update or delete on public.permission_notices
    for each row execute function public._permission_registry_append_only();
create trigger permission_grants_append_only
    before update or delete on public.permission_grants
    for each row execute function public._permission_registry_append_only();

do $tables_acl$
declare
    v_table text;
    v_role text;
begin
    foreach v_table in array array['permission_notices', 'permission_grants'] loop
        execute format('alter table public.%I enable row level security', v_table);
        execute format('revoke all on table public.%I from public', v_table);
        for v_role in select r.rolname from pg_roles r
                       where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname loop
            execute format('revoke all on table public.%I from %I', v_table, v_role);
        end loop;
    end loop;
end;
$tables_acl$;

-- ---------------------------------------------------------------------------
-- 3. Las RPC de service_role.
-- ---------------------------------------------------------------------------

-- El aviso, desde el manifiesto de la instancia ([[permisos.avisos]]). El mismo
-- aviso otra vez devuelve unchanged; otro contenido con el mismo notice_ref
-- aborta: un texto nuevo es una copy_version nueva y un notice_ref nuevo.
create function public.register_permission_notice_v1(p_notice jsonb)
returns table (notice_id uuid, outcome text)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_row public.permission_notices%rowtype;
    v_existing public.permission_notices%rowtype;
    v_offer jsonb;
begin
    if p_notice is null or jsonb_typeof(p_notice) <> 'object'
       or exists (select 1 from jsonb_object_keys(p_notice) key
                   where key not in ('tenant_ref', 'notice_ref', 'connector', 'product_ref', 'offer_refs',
                                     'uses', 'surface_url', 'copy_version', 'notice_text', 'notice_text_sha256',
                                     'checkbox', 'measured_at', 'measured_by', 'valid_from', 'valid_until',
                                     'requires_src'))
       or jsonb_typeof(p_notice -> 'uses') is distinct from 'array'
       or (p_notice ? 'offer_refs' and jsonb_typeof(p_notice -> 'offer_refs') is distinct from 'array') then
        raise exception using errcode = '22023', message = 'permission_notice_invalid_arguments';
    end if;
    for v_offer in select value from jsonb_array_elements(coalesce(p_notice -> 'offer_refs', '[]'::jsonb)) loop
        if jsonb_typeof(v_offer) <> 'string' or v_offer #>> '{}' !~ '^[A-Za-z0-9_-]{1,64}$' then
            raise exception using errcode = '22023', message = 'permission_notice_invalid_arguments';
        end if;
    end loop;
    begin
        v_row.tenant_ref := p_notice ->> 'tenant_ref';
        v_row.notice_ref := p_notice ->> 'notice_ref';
        v_row.connector := p_notice ->> 'connector';
        v_row.product_ref := p_notice ->> 'product_ref';
        v_row.offer_refs := coalesce(array(select jsonb_array_elements_text(p_notice -> 'offer_refs')
                                            order by 1), array[]::text[]);
        v_row.uses := array(select distinct jsonb_array_elements_text(p_notice -> 'uses') order by 1);
        v_row.surface_url := p_notice ->> 'surface_url';
        v_row.copy_version := p_notice ->> 'copy_version';
        v_row.notice_text := p_notice ->> 'notice_text';
        v_row.notice_text_sha256 := p_notice ->> 'notice_text_sha256';
        v_row.checkbox := p_notice ->> 'checkbox';
        v_row.measured_at := (p_notice ->> 'measured_at')::timestamptz;
        v_row.measured_by := p_notice ->> 'measured_by';
        v_row.valid_from := (p_notice ->> 'valid_from')::timestamptz;
        v_row.valid_until := (p_notice ->> 'valid_until')::timestamptz;
        v_row.requires_src := p_notice ->> 'requires_src';
    exception when invalid_datetime_format or datetime_field_overflow or invalid_text_representation then
        raise exception using errcode = '22023', message = 'permission_notice_invalid_arguments';
    end;

    select notice.* into v_existing
      from public.permission_notices notice
     where notice.tenant_ref = v_row.tenant_ref and notice.notice_ref = v_row.notice_ref
       for update;
    if found then
        if v_existing.connector is distinct from v_row.connector
           or v_existing.product_ref is distinct from v_row.product_ref
           or v_existing.offer_refs is distinct from v_row.offer_refs
           or v_existing.uses is distinct from v_row.uses
           or v_existing.surface_url is distinct from v_row.surface_url
           or v_existing.copy_version is distinct from v_row.copy_version
           or v_existing.notice_text_sha256 is distinct from v_row.notice_text_sha256
           or v_existing.checkbox is distinct from v_row.checkbox
           or v_existing.measured_at is distinct from v_row.measured_at
           or v_existing.valid_from is distinct from v_row.valid_from
           or v_existing.requires_src is distinct from v_row.requires_src then
            raise exception using errcode = '55000', message = 'permission_notice_conflict';
        end if;
        -- El cierre no viaja por aca: un aviso ya cerrado sigue cerrado.
        return query select v_existing.id, 'unchanged'::text;
        return;
    end if;

    begin
        insert into public.permission_notices (
            tenant_ref, notice_ref, connector, product_ref, offer_refs, uses, surface_url, copy_version,
            notice_text, notice_text_sha256, checkbox, measured_at, measured_by, valid_from, valid_until,
            requires_src)
        values (
            v_row.tenant_ref, v_row.notice_ref, v_row.connector, v_row.product_ref, v_row.offer_refs, v_row.uses,
            v_row.surface_url, v_row.copy_version, v_row.notice_text, v_row.notice_text_sha256, v_row.checkbox,
            v_row.measured_at, v_row.measured_by, v_row.valid_from, v_row.valid_until, v_row.requires_src)
        returning id into v_row.id;
    exception when check_violation or not_null_violation then
        raise exception using errcode = '22023', message = 'permission_notice_invalid_arguments';
    end;
    return query select v_row.id, 'inserted'::text;
end;
$function$;

-- El cierre del aviso (el monitor o una persona). Desde valid_until no nacen
-- permisos nuevos; los otorgados siguen valiendo.
create function public.close_permission_notice_v1(
    p_tenant_ref text, p_notice_ref text, p_valid_until timestamptz)
returns table (notice_id uuid, outcome text)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_notice public.permission_notices%rowtype;
begin
    if p_tenant_ref is null or p_notice_ref is null or p_valid_until is null then
        raise exception using errcode = '22023', message = 'permission_notice_invalid_arguments';
    end if;
    select notice.* into v_notice
      from public.permission_notices notice
     where notice.tenant_ref = p_tenant_ref and notice.notice_ref = p_notice_ref
       for update;
    if not found then
        return query select null::uuid, 'not_found'::text;
        return;
    end if;
    if v_notice.valid_until is not null then
        return query select v_notice.id, 'already_closed'::text;
        return;
    end if;
    if p_valid_until < v_notice.valid_from then
        raise exception using errcode = '22023', message = 'permission_notice_invalid_arguments';
    end if;
    update public.permission_notices set valid_until = p_valid_until where id = v_notice.id;
    return query select v_notice.id, 'closed'::text;
end;
$function$;

-- El permiso, desde un conector. Lo valida contra su aviso: del conector, de la
-- instancia y del producto, vigente al otorgarse, con la oferta y los usos
-- cubiertos, y la misma copy_version. Lo que no cubre no es un error del
-- conector: devuelve rejected con el motivo y no escribe.
create function public.record_permission_grant_v1(p_grant jsonb)
returns table (grant_id uuid, outcome text, reason text)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_notice public.permission_notices%rowtype;
    v_tenant text;
    v_connector text;
    v_evidence uuid;
    v_phone text;
    v_offer text;
    v_uses text[];
    v_contact uuid;
    v_granted timestamptz;
    v_expires timestamptz;
    v_id uuid;
begin
    if p_grant is null or jsonb_typeof(p_grant) <> 'object'
       or exists (select 1 from jsonb_object_keys(p_grant) key
                   where key not in ('tenant_ref', 'phone', 'contact_id', 'normalized_email', 'product_ref',
                                     'offer_ref', 'uses', 'connector', 'connector_version', 'evidence_kind',
                                     'evidence_ref', 'notice_ref', 'copy_version', 'granted_at', 'expires_at'))
       or jsonb_typeof(p_grant -> 'uses') is distinct from 'array' then
        raise exception using errcode = '22023', message = 'permission_grant_invalid_arguments';
    end if;
    begin
        v_tenant := p_grant ->> 'tenant_ref';
        v_connector := p_grant ->> 'connector';
        v_evidence := (p_grant ->> 'evidence_ref')::uuid;
        v_contact := (p_grant ->> 'contact_id')::uuid;
        v_granted := (p_grant ->> 'granted_at')::timestamptz;
        v_expires := (p_grant ->> 'expires_at')::timestamptz;
    exception when invalid_datetime_format or datetime_field_overflow or invalid_text_representation then
        raise exception using errcode = '22023', message = 'permission_grant_invalid_arguments';
    end;
    v_phone := public._whatsapp_phone_canonical(p_grant ->> 'phone');
    v_offer := nullif(btrim(p_grant ->> 'offer_ref'), '');
    v_uses := array(select distinct jsonb_array_elements_text(p_grant -> 'uses') order by 1);
    if v_tenant is null or v_connector is null or v_evidence is null or v_granted is null or v_expires is null
       or v_phone is null or v_phone !~ '^[0-9]{8,15}$'
       or p_grant ->> 'notice_ref' is null or p_grant ->> 'product_ref' is null
       or cardinality(v_uses) = 0 then
        raise exception using errcode = '22023', message = 'permission_grant_invalid_arguments';
    end if;

    -- El mismo acto otra vez (Hotmart reenvia): el permiso que ya esta.
    select existing.id into v_id
      from public.permission_grants existing
     where existing.tenant_ref = v_tenant and existing.connector = v_connector
       and existing.evidence_ref = v_evidence;
    if found then
        return query select v_id, 'duplicate'::text, null::text;
        return;
    end if;

    select notice.* into v_notice
      from public.permission_notices notice
     where notice.tenant_ref = v_tenant and notice.notice_ref = p_grant ->> 'notice_ref'
       for share;
    if not found then
        return query select null::uuid, 'rejected'::text, 'aviso_desconocido'::text;
        return;
    end if;
    if v_notice.connector is distinct from v_connector then
        return query select null::uuid, 'rejected'::text, 'aviso_de_otro_conector'::text;
        return;
    end if;
    if v_notice.product_ref is distinct from p_grant ->> 'product_ref' then
        return query select null::uuid, 'rejected'::text, 'aviso_de_otro_producto'::text;
        return;
    end if;
    if v_notice.copy_version is distinct from p_grant ->> 'copy_version' then
        return query select null::uuid, 'rejected'::text, 'aviso_de_otra_version'::text;
        return;
    end if;
    if v_granted < v_notice.valid_from
       or (v_notice.valid_until is not null and v_granted >= v_notice.valid_until) then
        return query select null::uuid, 'rejected'::text, 'aviso_no_vigente'::text;
        return;
    end if;
    if cardinality(v_notice.offer_refs) > 0 and (v_offer is null or not v_offer = any(v_notice.offer_refs)) then
        return query select null::uuid, 'rejected'::text, 'oferta_fuera_del_aviso'::text;
        return;
    end if;
    if not v_uses <@ v_notice.uses then
        return query select null::uuid, 'rejected'::text, 'uso_fuera_del_aviso'::text;
        return;
    end if;

    begin
        insert into public.permission_grants (
            tenant_ref, normalized_phone, contact_id, normalized_email, product_ref, offer_ref, uses, connector,
            connector_version, evidence_kind, evidence_ref, notice_ref, copy_version, granted_at, expires_at)
        values (
            v_tenant, v_phone, v_contact, nullif(lower(btrim(p_grant ->> 'normalized_email')), ''),
            v_notice.product_ref, v_offer, v_uses, v_connector, p_grant ->> 'connector_version',
            p_grant ->> 'evidence_kind', v_evidence, v_notice.notice_ref, v_notice.copy_version, v_granted, v_expires)
        on conflict (tenant_ref, connector, evidence_ref) do nothing
        returning id into v_id;
    exception when check_violation or not_null_violation or foreign_key_violation then
        raise exception using errcode = '22023', message = 'permission_grant_invalid_arguments';
    end;
    if v_id is null then
        select existing.id into v_id
          from public.permission_grants existing
         where existing.tenant_ref = v_tenant and existing.connector = v_connector
           and existing.evidence_ref = v_evidence;
        return query select v_id, 'duplicate'::text, null::text;
        return;
    end if;
    return query select v_id, 'inserted'::text, null::text;
end;
$function$;

-- La revocacion explicita (un aviso que se declaro por error, el pedido de la
-- persona). La baja de WhatsApp no se copia aca: el nucleo la sigue mirando en
-- contact_opt_out_events y en el contacto.
create function public.revoke_permission_grant_v1(p_grant_id uuid, p_revoked_by text)
returns table (grant_id uuid, outcome text)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_grant public.permission_grants%rowtype;
begin
    if p_grant_id is null or p_revoked_by is null or char_length(btrim(p_revoked_by)) not between 1 and 200 then
        raise exception using errcode = '22023', message = 'permission_grant_invalid_arguments';
    end if;
    select permission.* into v_grant from public.permission_grants permission where permission.id = p_grant_id
       for update;
    if not found then
        return query select null::uuid, 'not_found'::text;
        return;
    end if;
    if v_grant.revoked_at is not null then
        return query select v_grant.id, 'already_revoked'::text;
        return;
    end if;
    update public.permission_grants
       set revoked_at = clock_timestamp(), revoked_by = btrim(p_revoked_by)
     where id = v_grant.id;
    return query select v_grant.id, 'revoked'::text;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 4. La pregunta del nucleo.
-- ---------------------------------------------------------------------------
create function public._registered_permission_reason(
    p_tenant_ref text, p_contact_id uuid, p_destination text, p_product_ref text, p_offer text,
    p_at timestamptz, p_use text,
    out reason text, out permission_grant_id uuid)
returns record
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- permission_registry: la pregunta del nucleo (20261116000200).
    v_phone text;
    v_grant public.permission_grants%rowtype;
    v_binding public.commercial_ally_runtime_bindings%rowtype;
begin
    reason := 'no_entra:permiso';
    permission_grant_id := null;
    v_phone := public._whatsapp_phone_canonical(p_destination);
    if p_tenant_ref is null or p_contact_id is null or v_phone is null or p_product_ref is null
       or p_at is null or p_use is null then
        return;
    end if;
    -- El permiso vigente mas nuevo de ese telefono, producto y oferta (o de todo
    -- el producto), para ese uso, sin revocar.
    select permission.* into v_grant
      from public.permission_grants permission
     where permission.tenant_ref = p_tenant_ref
       and permission.normalized_phone = v_phone
       and permission.product_ref = p_product_ref
       and (permission.offer_ref is null or permission.offer_ref = p_offer)
       and p_use = any(permission.uses)
       and permission.granted_at <= p_at and p_at < permission.expires_at
       and permission.revoked_at is null
       and (permission.contact_id is null or permission.contact_id = p_contact_id)
     order by permission.granted_at desc, permission.id
     limit 1;
    if not found then
        return;
    end if;
    -- El contacto tiene ese movil, y el envio sale a contacts.phone: el mismo
    -- criterio que el formulario (_portable_consented_intent_reason).
    if not exists (
           select 1 from public.contact_points point
            where point.contact_id = p_contact_id and point.type = 'phone'
              and point.normalized_value = any(public._whatsapp_phone_variants(p_destination)))
       or not exists (
           select 1 from public.contacts contact
            where contact.id = p_contact_id
              and public._whatsapp_phone_canonical(contact.phone) = v_phone) then
        return;
    end if;
    select binding.* into v_binding
      from public.commercial_ally_runtime_bindings binding
     where binding.tenant_ref = p_tenant_ref and binding.funnel_ref = p_product_ref
       and binding.status = 'active';
    if not found then
        return;
    end if;
    -- La baja de ese movil en Chatwoot, la de siempre (sin importar si es
    -- anterior o posterior al permiso, como con el formulario).
    if exists (
        select 1 from public.contact_opt_out_events optout
         where optout.source = 'chatwoot' and optout.channel = 'whatsapp'
           and optout.canonical_account_id = v_binding.chatwoot_account_id
           and optout.external_user_id = any(public._whatsapp_phone_variants(p_destination))
           and optout.correlation_status in ('applied', 'unmatched', 'ambiguous', 'evidence_conflict')) then
        reason := 'baja';
        return;
    end if;
    reason := null;
    permission_grant_id := v_grant.id;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 5. El envoltorio del carrito y del pago rechazado, y la puerta.
-- ---------------------------------------------------------------------------
create function public._flow_engine_native_registered_audience(
    p_webhook_event_id uuid, p_contact_id uuid, p_offer text, p_destination text, p_tenant_ref text,
    p_product_ref text, p_at timestamptz,
    out reason text, out permission_grant_id uuid)
returns record
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- permission_registry: el carrito y el pago rechazado sin formulario (20261116000200).
    v_event public.webhook_events%rowtype;
    v_details public.commercial_ally_payment_failure_details%rowtype;
begin
    reason := 'no_entra:permiso';
    permission_grant_id := null;
    select event_row.* into v_event
      from public.webhook_events event_row
     where event_row.id = p_webhook_event_id and event_row.source = 'hotmart'
       and event_row.event_type in ('PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED');
    if not found or p_offer is null then
        return;
    end if;
    -- El evento es de la instancia, del producto del flujo y de esa oferta (la
    -- procedencia del binding portable).
    if not exists (
        select 1 from public.commercial_ally_hotmart_event_bindings provenance
         where provenance.webhook_event_id = p_webhook_event_id
           and provenance.tenant_ref = p_tenant_ref
           and provenance.funnel_ref = p_product_ref
           and provenance.offer_ref = p_offer) then
        return;
    end if;
    -- Un pendiente cancelado no es un rechazo, como en las otras dos ramas.
    if v_event.event_type = 'PURCHASE_CANCELED' then
        select details.* into v_details
          from public.commercial_ally_payment_failure_details details
         where details.webhook_event_id = p_webhook_event_id
           and details.trigger_kind = 'payment_failure';
        if not found then
            return;
        end if;
        if public.payment_failure_refusal_kind(v_details.refusal_reason,
               v_event.payload #>> '{data,purchase,payment,type}') = 'not_a_refusal' then
            reason := 'no_es_un_rechazo';
            return;
        end if;
    end if;
    select registered.reason, registered.permission_grant_id into reason, permission_grant_id
      from public._registered_permission_reason(p_tenant_ref, p_contact_id, p_destination, p_product_ref,
               p_offer, p_at, 'recuperacion') registered;
end;
$function$;

create function public._registered_permission_recheck(
    p_grant_id uuid, p_contact_id uuid, p_destination text, p_chatwoot_account_id bigint)
returns text
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- permission_registry: lo que mira la puerta antes de cada envio (20261116000200).
    v_grant public.permission_grants%rowtype;
begin
    select permission.* into v_grant
      from public.permission_grants permission
     where permission.id = p_grant_id
       for share;
    if not found
       or public._whatsapp_phone_canonical(p_destination) is distinct from v_grant.normalized_phone then
        return 'no_entra:permiso';
    end if;
    if v_grant.revoked_at is not null then
        return 'ya_no_entra';
    end if;
    if exists (
           select 1 from public.contacts contact_row
            where contact_row.id = p_contact_id
              and (contact_row.contact_permission in ('opted_out', 'blocked', 'restricted')
                   or contact_row.lifecycle_status = 'do_not_contact'))
       or public._portable_chatwoot_opt_out_stop(p_chatwoot_account_id, p_contact_id, p_destination) then
        return 'baja';
    end if;
    return null;
end;
$function$;

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public.register_permission_notice_v1(jsonb)',
        'public.close_permission_notice_v1(text,text,timestamptz)',
        'public.record_permission_grant_v1(jsonb)',
        'public.revoke_permission_grant_v1(uuid,text)',
        'public._permission_registry_append_only()',
        'public._registered_permission_reason(text,uuid,text,text,text,timestamptz,text)',
        'public._flow_engine_native_registered_audience(uuid,uuid,text,text,text,text,timestamptz)',
        'public._registered_permission_recheck(uuid,uuid,text,bigint)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in select r.rolname from pg_roles r
                       where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.register_permission_notice_v1(jsonb) to service_role;
        grant execute on function public.close_permission_notice_v1(text,text,timestamptz) to service_role;
        grant execute on function public.record_permission_grant_v1(jsonb) to service_role;
        grant execute on function public.revoke_permission_grant_v1(uuid,text) to service_role;
    end if;
end;
$acl$;

-- ---------------------------------------------------------------------------
-- 6. La fila del recorrido.
-- ---------------------------------------------------------------------------
alter table public.flow_engine_native_runs
    add column audience_permission_grant_id uuid references public.permission_grants(id) on delete restrict,
    drop constraint flow_engine_native_runs_audience_by_trigger,
    add constraint flow_engine_native_runs_audience_by_trigger check (
        -- permission_registry: el carrito o el pago rechazado con un permiso
        -- registrado, sin intencion (20261116000200).
        (audience_permission_grant_id is not null
         and not audience_hotmart_phone
         and trigger_event in ('carrito', 'pago_fallido')
         and audience_purchase_intent_id is null and audience_precheckout_submission_id is null)
        -- e483_payment_failure_by_phone: el pago rechazado de una oferta de
        -- campana, con el telefono de Hotmart y sin intencion (20261112210400).
        or (audience_permission_grant_id is null
            and audience_hotmart_phone
            and trigger_event = 'pago_fallido'
            and audience_purchase_intent_id is null and audience_precheckout_submission_id is null)
        -- e528_native_payment_slip: la ficha lleva su intencion, como el carrito (20261113200000).
        or (audience_permission_grant_id is null
            and not audience_hotmart_phone
            and trigger_event in ('carrito', 'pago_fallido', 'intencion', 'ficha_impresa')
            and audience_purchase_intent_id is not null and audience_precheckout_submission_id is not null)
        or (audience_permission_grant_id is null
            and not audience_hotmart_phone
            and trigger_event not in ('carrito', 'pago_fallido', 'intencion', 'ficha_impresa')
            and audience_purchase_intent_id is null and audience_precheckout_submission_id is null));

-- ---------------------------------------------------------------------------
-- 7 y 8. Los reemplazos sobre la definicion vigente.
-- ---------------------------------------------------------------------------
do $patch$
declare
    c_admit constant text :=
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)';
    c_tail constant text :=
        'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)';
begin
    -- 7. La admision: la variable.
    perform pg_temp.permission_registry_patch(c_admit,
        E'    v_audience_by_phone boolean := false;\n'
        || E'begin\n',
        E'    v_audience_by_phone boolean := false;\n'
        || E'    -- permission_registry_vars: el permiso registrado (20261116000200).\n'
        || E'    v_audience_grant uuid;\n'
        || E'begin\n',
        'permission_registry_vars');
    -- El carrito: despues del formulario, solo si el formulario no dio permiso.
    perform pg_temp.permission_registry_patch(c_admit,
        E'          from public._flow_engine_native_cart_audience(\n'
        || E'              p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref) audience;\n',
        E'          from public._flow_engine_native_cart_audience(\n'
        || E'              p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref) audience;\n'
        || E'        -- permission_registry_cart: begin (20261116000200)\n'
        || E'        -- Sin el permiso del formulario, el permiso registrado (un aviso en el\n'
        || E'        -- checkout o en la landing que un conector escribio en permission_grants).\n'
        || E'        if v_reason = ''no_entra:permiso'' then\n'
        || E'            select registered.reason, registered.permission_grant_id\n'
        || E'              into v_reason, v_audience_grant\n'
        || E'              from public._flow_engine_native_registered_audience(\n'
        || E'                  p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref,\n'
        || E'                  p_control.product_ref, p_moment) registered;\n'
        || E'        end if;\n'
        || E'        -- permission_registry_cart: end\n',
        'permission_registry_cart: begin');
    -- El pago rechazado: despues del formulario y del telefono de la campana.
    perform pg_temp.permission_registry_patch(c_admit,
        E'        -- e483_payment_failure_by_phone_permission: end\n',
        E'        -- e483_payment_failure_by_phone_permission: end\n'
        || E'        -- permission_registry_payment_failure: begin (20261116000200)\n'
        || E'        if v_reason = ''no_entra:permiso'' and not v_audience_by_phone then\n'
        || E'            select registered.reason, registered.permission_grant_id\n'
        || E'              into v_reason, v_audience_grant\n'
        || E'              from public._flow_engine_native_registered_audience(\n'
        || E'                  p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref,\n'
        || E'                  p_control.product_ref, p_moment) registered;\n'
        || E'        end if;\n'
        || E'        -- permission_registry_payment_failure: end\n',
        'permission_registry_payment_failure: begin');
    -- La autorizacion: la evidencia es el permiso registrado.
    perform pg_temp.permission_registry_patch(c_admit,
        E'            case when v_audience_by_phone\n',
        E'            case when v_audience_grant is not null\n'
        || E'                 -- permission_registry_grant (20261116000200)\n'
        || E'                 then jsonb_build_object(''reason'', ''registered_permission'',\n'
        || E'                                         ''permission_grant_id'', v_audience_grant,\n'
        || E'                                         ''webhook_event_id'', p_event.id, ''offer'', p_offer,\n'
        || E'                                         ''recovery_case_id'', v_case_id)\n'
        || E'                 when v_audience_by_phone\n',
        'permission_registry_grant');
    -- El recorrido: el permiso.
    perform pg_temp.permission_registry_patch(c_admit,
        E'        audience_hotmart_phone)\n'
        || E'    values (v_case_id, public._flow_engine_native_trigger(v_definition.definition), p_offer,\n'
        || E'        p_account_id, p_inbox_id, v_audience_intent, v_audience_submission, v_audience_by_phone);\n',
        E'        audience_hotmart_phone,\n'
        || E'        -- permission_registry_run (20261116000200)\n'
        || E'        audience_permission_grant_id)\n'
        || E'    values (v_case_id, public._flow_engine_native_trigger(v_definition.definition), p_offer,\n'
        || E'        p_account_id, p_inbox_id, v_audience_intent, v_audience_submission, v_audience_by_phone,\n'
        || E'        v_audience_grant);\n',
        'permission_registry_run');

    -- 8. La puerta antes de cada envio.
    perform pg_temp.permission_registry_patch(c_tail,
        E'    -- e483_payment_failure_by_phone_recheck: end\n'
        || E'    -- e528_native_payment_slip_recheck: y la ficha (20261113200000).\n'
        || E'    if v_native.trigger_event in (''carrito'', ''pago_fallido'', ''ficha_impresa'') and not v_native.audience_hotmart_phone then\n',
        E'    -- e483_payment_failure_by_phone_recheck: end\n'
        || E'    -- permission_registry_recheck: begin (20261116000200)\n'
        || E'    -- El carrito o el pago rechazado que entro con un permiso registrado no\n'
        || E'    -- tiene intencion: la puerta mira el permiso (revocado: ya_no_entra) y la\n'
        || E'    -- baja (la del contacto y la de Chatwoot de ese movil).\n'
        || E'    if v_native.audience_permission_grant_id is not null\n'
        || E'       and public._registered_permission_recheck(v_native.audience_permission_grant_id,\n'
        || E'               p_case.contact_id, v_identity.external_user_id, v_native.chatwoot_account_id) is not null then\n'
        || E'        return query select ''rechazado''::text,\n'
        || E'            public._registered_permission_recheck(v_native.audience_permission_grant_id,\n'
        || E'                p_case.contact_id, v_identity.external_user_id, v_native.chatwoot_account_id),\n'
        || E'            null::uuid, p_control.generation, false;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    -- permission_registry_recheck: end\n'
        || E'    -- e528_native_payment_slip_recheck: y la ficha (20261113200000).\n'
        || E'    if v_native.trigger_event in (''carrito'', ''pago_fallido'', ''ficha_impresa'') and not v_native.audience_hotmart_phone\n'
        || E'       and v_native.audience_permission_grant_id is null then\n',
        'permission_registry_recheck: begin');
end;
$patch$;

commit;
