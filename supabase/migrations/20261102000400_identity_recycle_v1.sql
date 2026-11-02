-- identity-recycle-v1: reciclar un telefono de prueba por una sola puerta
-- auditada (F-147, PR 1 del diseno E-257).
-- Contrato: docs/contracts/identity-recycle-v1.md. Diseno:
-- productos/soporte-infoproductores/documentacion/2026-11-01-f147-diseno-reciclar-telefono-de-prueba.md
-- (vault del OS). Antecedente hecho a mano: E-253 (ATT1, 01/11), con
-- session_replication_role = replica, 29 filas y el respaldo en el VPS.
--
-- Por que. Un numero de prueba deja rastro (compras, carritos, formularios,
-- una baja, el «una vez por persona») y ese rastro activa los mismos frenos
-- que protegen a un cliente real: la prueba siguiente no corre. Ningun freno
-- lee «la persona»: todos cruzan VALORES de telefono o de mail de la historia
-- contra los puntos de contacto vivos, en muchos lugares sin una funcion
-- comun. Por eso el modelo es A (D1): la identidad se reescribe en su origen,
-- a un marcador que no cruza con nadie, y cualquier lector, el de hoy y el de
-- manana, ve lo mismo sin cambiar.
--
-- Que trae.
-- 1. Tres tablas nuevas, vacias, con RLS y sin permisos para la API:
--    identity_recycles (un reciclado: instancia, actor de Slack, estado
--    aplicando/aplicado/deshaciendo/deshecho/purgado, la transaccion que lo
--    esta escribiendo, la huella de la vista previa, los conteos y los ultimos
--    dos digitos), identity_recycle_rows (el diario: una fila por celda
--    reescrita, con el valor viejo y el marcador) e identity_recycle_activity
--    (los ids que el reciclado registra como prueba).
-- 2. La excepcion con nombre en las dos funciones de inmutabilidad
--    (protect_hotmart_purchase_intent_correlation, solo para
--    hotmart_purchase_intent_event_identities, y
--    protect_hotmart_cart_source_event), SIN session_replication_role: un
--    UPDATE pasa solo si hay un reciclado aplicando o deshaciendo creado en
--    ESTA transaccion (txid_current() en la fila), existe la fila del diario
--    de cada celda con el valor de partida, y la fila nueva es identica a la
--    vieja salvo esas celdas, que pasan exactamente al marcador (o al valor
--    viejo, al deshacer). Un DELETE nunca pasa. Las tablas del reciclado solo
--    las escriben estas funciones (security definer): ningun rol de la API
--    tiene permisos, asi que un set_config no alcanza para falsificarlo.
-- 3. Las internas: el inventario, aplicar (guardas, diario, cambios y las
--    verificaciones por presencia antes del COMMIT), deshacer y la purga.
-- 4. Los envoltorios del aprobador, con la sesion viva de la pagina
--    (20261028000100): vista previa, aplicar con la huella de la vista,
--    releer un reciclado y deshacer. El actor sale de la sesion, nunca de un
--    parametro.
-- 5. purge_identity_recycles_v1() para service_role (el tick del bridge): a
--    las 72 h (D5), o cuando el telefono volvio a entrar, pone en nulo los
--    valores viejos del diario. Quedan el marcador, los ids y el actor.
-- 6. test_activity_ids_v1(): de solo lectura, los ids de prueba por tipo, sin
--    identidad. Sin permisos para la API: la leen los SQL de ventas del OS.
--
-- Que se reescribe (el inventario de E-253, generalizado a la instancia: las
-- cuentas de Chatwoot salen de sus bindings activos):
-- * telefono: contact_points de tipo phone (normalized_value y raw_value),
--   contacts.phone y channel_identities.external_user_id pasan a
--   reciclado:<reciclado8>:<fila8>; purchase_intents.normalized_phone,
--   hotmart_purchase_intent_event_identities.normalized_phone,
--   webhook_events.payload {data,buyer,phone|checkout_phone} y
--   contact_opt_out_events.external_user_id (D4) pasan a 999 + el numero del
--   reciclado con 9 cifras (los CHECK piden digitos y el 999 no es de ningun
--   pais);
-- * mail (D3, con p_recycle_emails): los mails que son SOLO de los contactos
--   del telefono pasan a reciclado-<numero>-<n>@reciclado.invalid en
--   contact_points de tipo email, contacts.email,
--   purchase_intents.normalized_email,
--   hotmart_purchase_intent_event_identities.normalized_email y
--   webhook_events.payload {data,buyer,email}. Un mail que comparte otro
--   contacto no se toca.
-- No se toca: precheckout_submissions, inbound_commercial_case_admissions,
-- los casos, recorridos, cohortes y correlaciones (cuelgan del contacto viejo
-- por id) y el do_not_contact del contacto viejo.
--
-- Las guardas abortan sin cambiar nada y vuelven como {"ok": false,
-- "reason_code"}: una fila de otra persona (un mail ajeno junto al telefono,
-- o un telefono ajeno junto a un mail que se recicla), el numero en otro
-- formato u otra ruta, casos abiertos o envios pendientes de los contactos,
-- una intencion viva, el mail nuevo ya conocido, ninguna huella de prueba
-- (D2: ningun contacto del telefono esta o estuvo en una cohorte) y la vista
-- vencida (la huella de la vista previa no es la de ahora). Las
-- verificaciones despues de los cambios (las filas por tabla en cero, el
-- numero en ningun formato, y los nueve frenos de E-253 para una persona
-- nueva con el telefono, con el mail nuevo y con los mails reciclados) corren
-- antes del COMMIT: si una falla, 55000 y no cambia nada.
--
-- Como. Las dos funciones de inmutabilidad se redefinen desde su definicion
-- VIGENTE (pg_get_functiondef) con un reemplazo contado, el metodo de
-- 20261101000200 y 20261101000300: el ancla tiene que aparecer exactamente
-- una vez y ninguna la marca de esta. Si algo no coincide, aborta con 55000 y
-- no cambia nada. create or replace conserva dueno, security definer,
-- search_path y permisos. No cambia ninguna firma ni ningun permiso de hoy, y
-- no cambia filas. Correrla dos veces aborta con 55000.
--
-- Errores: 22023 entrada invalida; 23514 una escritura que la excepcion no
-- deja pasar; 28000 sesion invalida (flow_approval_session_invalid); P0002 no
-- existe o no es de esta instancia; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_store_actor_is_valid(text,text)') is null
       or to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regprocedure('public._whatsapp_phone_variants(text)') is null
       or to_regprocedure('public._whatsapp_phone_canonical(text)') is null
       or to_regprocedure('public._portable_checkout_link_purchase_exists(text,text,uuid,text,text)') is null
       or to_regprocedure('public.get_portable_person_activity_v1(text,text,text)') is null
       or to_regprocedure('public.protect_hotmart_purchase_intent_correlation()') is null
       or to_regprocedure('public.protect_hotmart_cart_source_event()') is null
       or to_regclass('public.flow_cohort_members') is null
       or to_regclass('public.pilot_cohort_memberships') is null then
        raise exception using errcode = '55000', message = 'identity_recycle_prerequisite_missing';
    end if;
    if to_regclass('public.identity_recycles') is not null then
        raise exception using errcode = '55000', message = 'identity_recycle_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. Las tablas.
-- ===========================================================================

create table public.identity_recycles (
    id uuid primary key default gen_random_uuid(),
    -- El numero del reciclado: el que ve el aprobador y el que llevan los
    -- marcadores de digitos (999 + 9 cifras).
    recycle_number bigint generated always as identity unique
        check (recycle_number between 1 and 999999999),
    tenant_ref text not null,
    ally_ref text not null,
    actor_kind text not null,
    actor_ref text not null,
    status text not null check (status in ('aplicando', 'aplicado', 'deshaciendo', 'deshecho', 'purgado')),
    -- La transaccion que lo esta escribiendo: la excepcion de inmutabilidad
    -- solo vale dentro de ella.
    txid bigint not null,
    fingerprint text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
    phone_last2 text not null check (phone_last2 ~ '^[0-9]{2}$'),
    recycle_emails boolean not null,
    counts jsonb not null default '{}'::jsonb check (jsonb_typeof(counts) = 'object'),
    created_at timestamptz not null default clock_timestamp(),
    applied_at timestamptz,
    undone_at timestamptz,
    undone_by_kind text,
    undone_by_ref text,
    purged_at timestamptz,
    check (public._flow_store_actor_is_valid(actor_kind, actor_ref)),
    check ((undone_by_kind is null) = (undone_by_ref is null)),
    check (undone_by_kind is null or public._flow_store_actor_is_valid(undone_by_kind, undone_by_ref)),
    check (status = 'aplicando' or applied_at is not null),
    check ((status = 'deshecho') = (undone_at is not null)),
    check (status <> 'purgado' or purged_at is not null)
);

create index identity_recycles_instance_idx
    on public.identity_recycles (tenant_ref, ally_ref, created_at desc);

-- El diario: una fila por celda reescrita. old_value queda en nulo con la
-- purga; el marcador, la tabla y el id quedan para auditar.
create table public.identity_recycle_rows (
    recycle_id uuid not null references public.identity_recycles(id) on delete restrict,
    table_name text not null check (table_name in (
        'contact_points', 'contacts', 'channel_identities', 'purchase_intents',
        'hotmart_purchase_intent_event_identities', 'webhook_events', 'contact_opt_out_events')),
    row_id uuid not null,
    column_name text not null check (column_name in (
        'normalized_value', 'raw_value', 'phone', 'email', 'external_user_id', 'normalized_phone',
        'normalized_email', 'payload.data.buyer.phone', 'payload.data.buyer.checkout_phone',
        'payload.data.buyer.email')),
    kind text not null check (kind in ('phone', 'email')),
    old_value text,
    marker text not null check (marker ~ '^(reciclado:[0-9a-f]{8}:[0-9a-f]{8}|999[0-9]{9}|reciclado-[0-9]+-[0-9]+@reciclado\.invalid)$'),
    created_at timestamptz not null default clock_timestamp(),
    primary key (recycle_id, table_name, row_id, column_name)
);

-- Los ids que el reciclado registra como prueba: los que reescribio y los que
-- cuelgan del contacto viejo y no se tocan.
create table public.identity_recycle_activity (
    recycle_id uuid not null references public.identity_recycles(id) on delete restrict,
    kind text not null check (kind in (
        'contact', 'commercial_case', 'recovery_case', 'purchase_intent', 'webhook_event',
        'checkout_link_issuance')),
    ref_id uuid not null,
    primary key (recycle_id, kind, ref_id)
);

create index identity_recycle_activity_ref_idx on public.identity_recycle_activity (kind, ref_id);

alter table public.identity_recycles enable row level security;
alter table public.identity_recycle_rows enable row level security;
alter table public.identity_recycle_activity enable row level security;

-- Las tres tablas solo cambian como lo pide el ciclo: un reciclado nace
-- aplicando en la transaccion que lo escribe, y el diario y la actividad
-- solo se agregan mientras tanto. Ninguna se borra.
create function public.guard_identity_recycle_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op in ('DELETE', 'TRUNCATE') then
        raise exception using errcode = '23514', message = 'identity_recycle_immutable';
    end if;
    if tg_op = 'INSERT' then
        if new.status <> 'aplicando' or new.txid <> txid_current() or new.applied_at is not null
           or new.undone_at is not null or new.purged_at is not null then
            raise exception using errcode = '23514', message = 'identity_recycle_transition_invalid';
        end if;
        return new;
    end if;
    if new.id is distinct from old.id or new.recycle_number is distinct from old.recycle_number
       or new.tenant_ref is distinct from old.tenant_ref or new.ally_ref is distinct from old.ally_ref
       or new.actor_kind is distinct from old.actor_kind or new.actor_ref is distinct from old.actor_ref
       or new.fingerprint is distinct from old.fingerprint or new.phone_last2 is distinct from old.phone_last2
       or new.recycle_emails is distinct from old.recycle_emails or new.created_at is distinct from old.created_at then
        raise exception using errcode = '23514', message = 'identity_recycle_immutable';
    end if;
    if not (
        (old.status = new.status and old.status not in ('aplicando', 'deshaciendo')
         and new.txid = old.txid and new.counts = old.counts
         and new.applied_at is not distinct from old.applied_at
         and new.undone_at is not distinct from old.undone_at
         and new.undone_by_kind is not distinct from old.undone_by_kind
         and new.undone_by_ref is not distinct from old.undone_by_ref
         and (old.purged_at is null or new.purged_at = old.purged_at))
        or (old.status = 'aplicando' and new.status = 'aplicando' and new.txid = old.txid
            and old.txid = txid_current())
        or (old.status = 'aplicando' and new.status = 'aplicado' and new.txid = old.txid
            and old.txid = txid_current())
        or (old.status = 'aplicado' and new.status = 'deshaciendo' and new.txid = txid_current()
            and new.counts = old.counts)
        or (old.status = 'deshaciendo' and new.status = 'deshecho' and new.txid = old.txid
            and old.txid = txid_current() and new.counts = old.counts)
        or (old.status = 'aplicado' and new.status = 'purgado' and new.txid = old.txid
            and new.counts = old.counts)
    ) then
        raise exception using errcode = '23514', message = 'identity_recycle_transition_invalid';
    end if;
    return new;
end;
$function$;

create function public.guard_identity_recycle_journal_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op in ('DELETE', 'TRUNCATE') then
        raise exception using errcode = '23514', message = 'identity_recycle_immutable';
    end if;
    if tg_op = 'INSERT' then
        if not exists (
            select 1 from public.identity_recycles r
             where r.id = new.recycle_id and r.status = 'aplicando' and r.txid = txid_current()
        ) then
            raise exception using errcode = '23514', message = 'identity_recycle_journal_closed';
        end if;
        return new;
    end if;
    -- La unica escritura posterior es la purga en el diario: el valor viejo
    -- pasa a nulo y nada mas cambia.
    if tg_table_name <> 'identity_recycle_rows'
       or old.old_value is null or new.old_value is not null
       or (to_jsonb(new) - 'old_value') <> (to_jsonb(old) - 'old_value') then
        raise exception using errcode = '23514', message = 'identity_recycle_immutable';
    end if;
    return new;
end;
$function$;

create trigger identity_recycles_guard
before insert or update or delete on public.identity_recycles
for each row execute function public.guard_identity_recycle_change();
create trigger identity_recycles_no_truncate
before truncate on public.identity_recycles
for each statement execute function public.guard_identity_recycle_change();
create trigger identity_recycle_rows_guard
before insert or update or delete on public.identity_recycle_rows
for each row execute function public.guard_identity_recycle_journal_change();
create trigger identity_recycle_rows_no_truncate
before truncate on public.identity_recycle_rows
for each statement execute function public.guard_identity_recycle_journal_change();
create trigger identity_recycle_activity_guard
before insert or update or delete on public.identity_recycle_activity
for each row execute function public.guard_identity_recycle_journal_change();
create trigger identity_recycle_activity_no_truncate
before truncate on public.identity_recycle_activity
for each statement execute function public.guard_identity_recycle_journal_change();

-- ===========================================================================
-- 2. La excepcion con nombre.
-- ===========================================================================

-- Si una escritura de una tabla inmutable es exactamente la que pide un
-- reciclado de ESTA transaccion: cada celda que cambia tiene su fila del
-- diario con el valor de partida, y fuera de esas celdas la fila nueva es la
-- vieja. Al aplicar, el valor de partida es el viejo y el de llegada el
-- marcador; al deshacer, al reves. Sin fila del diario, no pasa.
create function public._identity_recycle_allows(p_table text, p_old jsonb, p_new jsonb)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_recycle public.identity_recycles%rowtype;
    v_row record;
    v_expected jsonb := p_old;
    v_key uuid;
    v_path text[];
    v_from text;
    v_to text;
    v_seen integer := 0;
begin
    if p_table not in ('hotmart_purchase_intent_event_identities', 'webhook_events')
       or p_old is null or p_new is null then
        return false;
    end if;
    select r.* into v_recycle from public.identity_recycles r
     where r.txid = txid_current() and r.status in ('aplicando', 'deshaciendo')
     order by r.created_at desc limit 1;
    if not found then
        return false;
    end if;
    v_key := case when p_table = 'webhook_events' then (p_old ->> 'id')::uuid
                  else (p_old ->> 'webhook_event_id')::uuid end;
    for v_row in
        select j.column_name, j.old_value, j.marker from public.identity_recycle_rows j
         where j.recycle_id = v_recycle.id and j.table_name = p_table and j.row_id = v_key
         order by j.column_name
    loop
        if v_row.old_value is null then
            return false;
        end if;
        v_from := case when v_recycle.status = 'aplicando' then v_row.old_value else v_row.marker end;
        v_to := case when v_recycle.status = 'aplicando' then v_row.marker else v_row.old_value end;
        v_path := case when v_row.column_name like 'payload.%'
                       then array['payload'] || string_to_array(substr(v_row.column_name, 9), '.')
                       else array[v_row.column_name] end;
        if (v_expected #>> v_path) is distinct from v_from then
            return false;
        end if;
        v_expected := jsonb_set(v_expected, v_path, to_jsonb(v_to), false);
        v_seen := v_seen + 1;
    end loop;
    return v_seen > 0 and v_expected = p_new;
end;
$function$;

create or replace function pg_temp.identity_recycle_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'identity_recycle_owner_mismatch', detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'identity_recycle_already_applied', detail = p_signature;
    end if;
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor) then
        raise exception using errcode = '55000', message = 'unexpected_immutability_definition',
            detail = p_signature || ' ancla';
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'identity_recycle_not_replaced', detail = p_signature;
    end if;
end;
$function$;

do $patch$
declare
    c_identity_anchor constant text :=
        E'begin\n    raise exception using errcode = ''23514'', message = ''hotmart_purchase_intent_correlation_immutable'';\n';
    c_cart_anchor constant text :=
        E'        return old;\n    end if;\n\n    if (\n';
begin
    perform pg_temp.identity_recycle_patch(
        'public.protect_hotmart_purchase_intent_correlation()',
        c_identity_anchor,
        E'begin\n'
        || E'    -- identity_recycle_v1: begin\n'
        || E'    -- La unica escritura que pasa: la de un reciclado de esta transaccion,\n'
        || E'    -- celda por celda contra su diario (20261102000400). Solo esta tabla.\n'
        || E'    if tg_op = ''UPDATE'' and tg_table_name = ''hotmart_purchase_intent_event_identities''\n'
        || E'       and public._identity_recycle_allows(tg_table_name, to_jsonb(old), to_jsonb(new)) then\n'
        || E'        return new;\n'
        || E'    end if;\n'
        || E'    -- identity_recycle_v1: end\n'
        || E'    raise exception using errcode = ''23514'', message = ''hotmart_purchase_intent_correlation_immutable'';\n',
        'identity_recycle_v1: begin');
    perform pg_temp.identity_recycle_patch(
        'public.protect_hotmart_cart_source_event()',
        c_cart_anchor,
        E'        return old;\n    end if;\n\n'
        || E'    -- identity_recycle_v1: begin\n'
        || E'    -- La unica escritura que pasa: la de un reciclado de esta transaccion,\n'
        || E'    -- celda por celda contra su diario (20261102000400). Un DELETE no.\n'
        || E'    if public._identity_recycle_allows(''webhook_events'', to_jsonb(old), to_jsonb(new)) then\n'
        || E'        return new;\n'
        || E'    end if;\n'
        || E'    -- identity_recycle_v1: end\n\n'
        || E'    if (\n',
        'identity_recycle_v1: begin');
end;
$patch$;

-- ===========================================================================
-- 3. El inventario.
-- ===========================================================================

-- Los contactos del telefono: un punto de contacto, contacts.phone o una
-- identidad del canal con alguna de sus formas.
create function public._identity_recycle_contacts(p_forms text[])
returns uuid[]
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select coalesce(array_agg(distinct x.id order by x.id), array[]::uuid[]) from (
        select cp.contact_id id from public.contact_points cp
         where cp.type = 'phone'
           and (regexp_replace(cp.normalized_value, '\D', '', 'g') = any(p_forms)
                or regexp_replace(cp.raw_value, '\D', '', 'g') = any(p_forms))
        union
        select c.id from public.contacts c
         where regexp_replace(coalesce(c.phone, ''), '\D', '', 'g') = any(p_forms)
        union
        select ci.contact_id from public.channel_identities ci
         where regexp_replace(coalesce(ci.external_user_id, ''), '\D', '', 'g') = any(p_forms)
    ) x
$function$;

-- Las celdas a reescribir, con su problema si lo tiene (null = se puede):
-- fila_de_otra_persona (el mail de la fila no es de los contactos del
-- telefono, ni su base sin +etiqueta; o el telefono de la fila no es este en
-- una fila que se toca por su mail), u otro_formato_o_ruta (el valor no es un
-- texto que la funcion sepa reescribir y volver atras).
create function public._identity_recycle_targets(
    p_forms text[], p_accounts text[], p_contacts uuid[], p_same_emails text[], p_email_bases text[],
    p_recycle_emails text[])
returns table (table_name text, row_id uuid, column_name text, kind text, old_value text, problem text)
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    with same as (
        select e from unnest(p_same_emails) e
    ),
    phone_rows as (
        select 'contact_points'::text t, cp.id, c.col, 'phone'::text kind,
               case c.col when 'normalized_value' then cp.normalized_value else cp.raw_value end old_value,
               null::text problem
          from public.contact_points cp
          cross join (values ('normalized_value'), ('raw_value')) c(col)
         where cp.type = 'phone'
           and (regexp_replace(cp.normalized_value, '\D', '', 'g') = any(p_forms)
                or regexp_replace(cp.raw_value, '\D', '', 'g') = any(p_forms))
        union all
        select 'contacts', c.id, 'phone', 'phone', c.phone,
               case when exists (
                   select 1 from public.contact_points o
                    where o.contact_id = c.id and o.type = 'phone'
                      and regexp_replace(o.normalized_value, '\D', '', 'g') <> all(p_forms))
                    then 'fila_de_otra_persona' end
          from public.contacts c
         where regexp_replace(coalesce(c.phone, ''), '\D', '', 'g') = any(p_forms)
        union all
        select 'channel_identities', ci.id, 'external_user_id', 'phone', ci.external_user_id,
               case when ci.channel <> 'whatsapp' or not (ci.account_id = any(p_accounts))
                    then 'fila_de_otra_persona' end
          from public.channel_identities ci
         where regexp_replace(coalesce(ci.external_user_id, ''), '\D', '', 'g') = any(p_forms)
        union all
        select 'purchase_intents', i.id, 'normalized_phone', 'phone', i.normalized_phone,
               case when not (i.normalized_email is null or i.normalized_email in (select e from same)
                              or regexp_replace(i.normalized_email, '\+[^@]*@', '@') = any(p_email_bases))
                    then 'fila_de_otra_persona' end
          from public.purchase_intents i
         where regexp_replace(coalesce(i.normalized_phone, ''), '\D', '', 'g') = any(p_forms)
        union all
        select 'hotmart_purchase_intent_event_identities', h.webhook_event_id, 'normalized_phone', 'phone',
               h.normalized_phone,
               case when not (h.normalized_email is null or h.normalized_email in (select e from same)
                              or regexp_replace(h.normalized_email, '\+[^@]*@', '@') = any(p_email_bases))
                    then 'fila_de_otra_persona' end
          from public.hotmart_purchase_intent_event_identities h
         where regexp_replace(coalesce(h.normalized_phone, ''), '\D', '', 'g') = any(p_forms)
        union all
        select 'webhook_events', w.id, 'payload.data.buyer.' || p.leaf, 'phone', w.payload #>> array['data', 'buyer', p.leaf],
               case when jsonb_typeof(w.payload #> array['data', 'buyer', p.leaf]) <> 'string'
                    then 'otro_formato_o_ruta'
                    when w.source <> 'hotmart' or not (
                        nullif(lower(btrim(w.payload #>> '{data,buyer,email}')), '') is null
                        or lower(btrim(w.payload #>> '{data,buyer,email}')) in (select e from same)
                        or regexp_replace(lower(btrim(w.payload #>> '{data,buyer,email}')), '\+[^@]*@', '@')
                           = any(p_email_bases))
                    then 'fila_de_otra_persona' end
          from public.webhook_events w
          cross join (values ('phone'), ('checkout_phone')) p(leaf)
         where regexp_replace(coalesce(w.payload #>> array['data', 'buyer', p.leaf], ''), '\D', '', 'g')
               = any(p_forms)
        union all
        -- D4: la baja de la prueba la dio quien prueba. La del contacto viejo
        -- (do_not_contact) no se toca nunca.
        select 'contact_opt_out_events', o.id, 'external_user_id', 'phone', o.external_user_id,
               case when o.contact_id is not null and not (o.contact_id = any(p_contacts))
                    then 'fila_de_otra_persona' end
          from public.contact_opt_out_events o
         where o.external_user_id = any(p_forms)
    ),
    -- D3: los mails que son solo de los contactos del telefono, donde esten.
    -- Una fila con uno de esos mails y otro telefono no es de esta persona.
    email_rows as (
        select 'contact_points'::text t, cp.id, c.col, 'email'::text kind,
               case c.col when 'normalized_value' then cp.normalized_value else cp.raw_value end old_value,
               null::text problem
          from public.contact_points cp
          cross join (values ('normalized_value'), ('raw_value')) c(col)
         where cp.type = 'email' and cp.contact_id = any(p_contacts)
           and lower(btrim(cp.normalized_value)) = any(p_recycle_emails)
        union all
        select 'contacts', c.id, 'email', 'email', c.email, null
          from public.contacts c
         where c.id = any(p_contacts) and lower(btrim(c.email)) = any(p_recycle_emails)
        union all
        select 'purchase_intents', i.id, 'normalized_email', 'email', i.normalized_email,
               case when i.normalized_phone is not null
                         and regexp_replace(i.normalized_phone, '\D', '', 'g') <> all(p_forms)
                    then 'fila_de_otra_persona' end
          from public.purchase_intents i
         where i.normalized_email = any(p_recycle_emails)
        union all
        select 'hotmart_purchase_intent_event_identities', h.webhook_event_id, 'normalized_email', 'email',
               h.normalized_email,
               case when h.normalized_phone is not null
                         and regexp_replace(h.normalized_phone, '\D', '', 'g') <> all(p_forms)
                    then 'fila_de_otra_persona' end
          from public.hotmart_purchase_intent_event_identities h
         where h.normalized_email = any(p_recycle_emails)
        union all
        select 'webhook_events', w.id, 'payload.data.buyer.email', 'email', w.payload #>> '{data,buyer,email}',
               case when jsonb_typeof(w.payload #> '{data,buyer,email}') <> 'string' then 'otro_formato_o_ruta'
                    when w.source <> 'hotmart'
                         or exists (
                             select 1 from (values ('phone'), ('checkout_phone')) p(leaf)
                              where nullif(regexp_replace(coalesce(
                                        w.payload #>> array['data', 'buyer', p.leaf], ''), '\D', '', 'g'), '')
                                    is not null
                                and regexp_replace(w.payload #>> array['data', 'buyer', p.leaf], '\D', '', 'g')
                                    <> all(p_forms))
                    then 'fila_de_otra_persona' end
          from public.webhook_events w
         where lower(btrim(w.payload #>> '{data,buyer,email}')) = any(p_recycle_emails)
    )
    select t, id, col, kind, old_value, problem from phone_rows
    union all
    select t, id, col, kind, old_value, problem from email_rows
$function$;

-- Un payload con valores de texto puestos en sus rutas ({"payload.data.buyer.phone": "..."}):
-- la reescritura de webhook_events, de ida y de vuelta.
create function public._identity_recycle_set_paths(p_payload jsonb, p_values jsonb)
returns jsonb
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
declare
    v_out jsonb := p_payload;
    v_item record;
begin
    for v_item in select e.key as path, e.value as val from jsonb_each_text(p_values) e order by 1 loop
        v_out := jsonb_set(v_out, string_to_array(substr(v_item.path, 9), '.'), to_jsonb(v_item.val), false);
    end loop;
    return v_out;
end;
$function$;

-- Cuantas filas de las tablas del inventario tienen alguno de los valores
-- (los ultimos 10 digitos del telefono o un mail reciclado) fuera de las
-- celdas que se reescriben: el numero en otro formato o en otra ruta. Compara
-- con strpos, sin comodines.
create function public._identity_recycle_residue(p_tokens text[], p_targets jsonb)
returns bigint
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    with tok as (
        select distinct lower(v) v from unnest(p_tokens) v where nullif(v, '') is not null
    ),
    touched as (
        select x ->> 'table' t, (x ->> 'row_id')::uuid id, x ->> 'column' col from jsonb_array_elements(p_targets) x
    )
    select
        (select count(*) from public.contact_points cp
          where exists (select 1 from tok where strpos(lower(
                    case when exists (select 1 from touched where t = 'contact_points' and id = cp.id) then ''
                         else cp.normalized_value || ' ' || cp.raw_value end), tok.v) > 0))
      + (select count(*) from public.contacts c
          where exists (select 1 from tok where strpos(lower(
                    case when exists (select 1 from touched where t = 'contacts' and id = c.id and col = 'phone')
                         then '' else coalesce(c.phone, '') end || ' ' ||
                    case when exists (select 1 from touched where t = 'contacts' and id = c.id and col = 'email')
                         then '' else coalesce(c.email, '') end), tok.v) > 0))
      + (select count(*) from public.channel_identities ci
          where not exists (select 1 from touched where t = 'channel_identities' and id = ci.id)
            and exists (select 1 from tok where strpos(lower(coalesce(ci.external_user_id, '')), tok.v) > 0))
      + (select count(*) from public.purchase_intents i
          where exists (select 1 from tok where strpos(lower(
                    case when exists (select 1 from touched where t = 'purchase_intents' and id = i.id
                                                             and col = 'normalized_phone')
                         then '' else coalesce(i.normalized_phone, '') end || ' ' ||
                    case when exists (select 1 from touched where t = 'purchase_intents' and id = i.id
                                                             and col = 'normalized_email')
                         then '' else coalesce(i.normalized_email, '') end), tok.v) > 0))
      + (select count(*) from public.hotmart_purchase_intent_event_identities h
          where exists (select 1 from tok where strpos(lower(
                    case when exists (select 1 from touched where t = 'hotmart_purchase_intent_event_identities'
                                                             and id = h.webhook_event_id and col = 'normalized_phone')
                         then '' else coalesce(h.normalized_phone, '') end || ' ' ||
                    case when exists (select 1 from touched where t = 'hotmart_purchase_intent_event_identities'
                                                             and id = h.webhook_event_id and col = 'normalized_email')
                         then '' else coalesce(h.normalized_email, '') end), tok.v) > 0))
      + (select count(*) from public.contact_opt_out_events o
          where not exists (select 1 from touched where t = 'contact_opt_out_events' and id = o.id)
            and exists (select 1 from tok where strpos(o.external_user_id, tok.v) > 0))
      + (select count(*) from public.webhook_events w
          where exists (select 1 from tok where strpos(lower(
                    (w.payload
                     #- (case when exists (select 1 from touched where t = 'webhook_events' and id = w.id
                                                                  and col = 'payload.data.buyer.phone')
                              then array['data', 'buyer', 'phone'] else array['_no_'] end)
                     #- (case when exists (select 1 from touched where t = 'webhook_events' and id = w.id
                                                                  and col = 'payload.data.buyer.checkout_phone')
                              then array['data', 'buyer', 'checkout_phone'] else array['_no_'] end)
                     #- (case when exists (select 1 from touched where t = 'webhook_events' and id = w.id
                                                                  and col = 'payload.data.buyer.email')
                              then array['data', 'buyer', 'email'] else array['_no_'] end))::text), tok.v) > 0))
$function$;

-- Todo lo que el reciclado sabe de un telefono en una instancia. INTERNA:
-- trae el telefono y los mails en claro y no sale de la base; la vista del
-- aprobador es otra cosa (approver_identity_recycle_preview_v1).
create function public._identity_recycle_inventory(
    p_tenant_ref text, p_ally_ref text, p_phone text, p_new_email text, p_recycle_emails boolean)
returns jsonb
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_canonical text;
    v_forms text[];
    v_national text;
    v_accounts text[];
    v_funnels text[];
    v_contacts uuid[];
    v_emails text[];
    v_bases text[];
    v_exclusive text[];
    v_shared text[];
    v_recycled text[];
    v_targets jsonb;
    v_guards jsonb;
    v_residue integer;
    v_new_email text := nullif(lower(btrim(p_new_email)), '');
begin
    if p_phone is null or p_phone !~ '^[1-9][0-9]{9,14}$' then
        raise exception using errcode = '22023', message = 'identity_recycle_phone_invalid';
    end if;
    if v_new_email is not null and v_new_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' then
        raise exception using errcode = '22023', message = 'identity_recycle_email_invalid';
    end if;
    if p_recycle_emails is null then
        raise exception using errcode = '22023', message = 'identity_recycle_emails_flag_invalid';
    end if;
    v_canonical := public._whatsapp_phone_canonical(p_phone);
    v_forms := public._whatsapp_phone_variants(v_canonical);
    if v_forms is null or cardinality(v_forms) = 0 then
        raise exception using errcode = '22023', message = 'identity_recycle_phone_invalid';
    end if;
    v_national := right(v_canonical, 10);

    select coalesce(array_agg(distinct 'chatwoot:' || b.chatwoot_account_id::text), array[]::text[]),
           coalesce(array_agg(distinct b.funnel_ref), array[]::text[])
      into v_accounts, v_funnels
      from public.commercial_ally_runtime_bindings b
     where b.tenant_ref = p_tenant_ref and b.ally_ref = p_ally_ref and b.status = 'active';

    v_contacts := public._identity_recycle_contacts(v_forms);
    select coalesce(array_agg(distinct e order by e), array[]::text[]) into v_emails from (
        select lower(btrim(cp.normalized_value)) e from public.contact_points cp
         where cp.type = 'email' and cp.contact_id = any(v_contacts)
        union
        select lower(btrim(c.email)) from public.contacts c
         where c.id = any(v_contacts) and nullif(btrim(c.email), '') is not null
    ) x;
    select coalesce(array_agg(distinct regexp_replace(e, '\+[^@]*@', '@')), array[]::text[])
      into v_bases from unnest(v_emails) e;
    -- Un mail es solo de estos contactos si ningun otro lo tiene como punto
    -- ni como contacts.email (D3: el gmail de Dan con su otro telefono, en
    -- E-253, no se toca).
    select coalesce(array_agg(e order by e) filter (where not shared), array[]::text[]),
           coalesce(array_agg(e order by e) filter (where shared), array[]::text[])
      into v_exclusive, v_shared
      from (
          select e, exists (
                     select 1 from public.contact_points cp
                      where cp.type = 'email' and lower(btrim(cp.normalized_value)) = e
                        and not (cp.contact_id = any(v_contacts)))
                 or exists (
                     select 1 from public.contacts c
                      where lower(btrim(c.email)) = e and not (c.id = any(v_contacts))) shared
            from unnest(v_emails) e
      ) x;
    v_recycled := case when p_recycle_emails then v_exclusive else array[]::text[] end;

    select coalesce(jsonb_agg(jsonb_build_object('table', t.table_name, 'row_id', t.row_id, 'column', t.column_name,
               'kind', t.kind, 'old_value', t.old_value, 'problem', t.problem)
             order by t.table_name, t.row_id, t.column_name), '[]'::jsonb)
      into v_targets
      from public._identity_recycle_targets(v_forms, v_accounts, v_contacts, v_emails, v_bases, v_recycled) t;

    -- El numero (o un mail reciclado) en otro formato o en otra ruta: una fila
    -- de las tablas del inventario que lo contiene y que no se toca, o una
    -- fila que se toca y lo sigue teniendo fuera de las celdas que se
    -- reescriben.
    v_residue := public._identity_recycle_residue(array[v_national] || v_recycled, v_targets);

    v_guards := jsonb_build_object(
        'fila_de_otra_persona', (select count(*) from jsonb_array_elements(v_targets) x
                                  where x ->> 'problem' = 'fila_de_otra_persona'),
        'otro_formato_o_ruta', v_residue + (select count(*) from jsonb_array_elements(v_targets) x
                                             where x ->> 'problem' = 'otro_formato_o_ruta'),
        'casos_abiertos', (select count(*) from public.recovery_cases rc
                            where rc.contact_id = any(v_contacts) and rc.status in ('grace_period', 'active', 'paused')),
        'envios_pendientes', (select count(*) from public.scheduled_actions a
                                join public.recovery_cases rc on rc.id = a.recovery_case_id
                               where rc.contact_id = any(v_contacts)
                                 and a.status in ('pending', 'deferred', 'retryable_failed', 'delivery_unknown')),
        'intenciones_vivas', (select count(*) from public.purchase_intents i
                               where i.lifecycle_state = 'waiting_for_purchase'
                                 and i.id in (select (x ->> 'row_id')::uuid from jsonb_array_elements(v_targets) x
                                               where x ->> 'table' = 'purchase_intents')),
        'mail_nuevo_ya_conocido', case when v_new_email is null then 0 else
            (select count(*) from public.contact_points cp where cp.type = 'email' and lower(btrim(cp.normalized_value)) = v_new_email)
          + (select count(*) from public.contacts c where lower(btrim(c.email)) = v_new_email)
          + (select count(*) from public.purchase_intents i where i.normalized_email = v_new_email)
          + (select count(*) from public.hotmart_purchase_intent_event_identities h where h.normalized_email = v_new_email)
          + (select count(*) from public.webhook_events w where lower(btrim(w.payload #>> '{data,buyer,email}')) = v_new_email)
          end,
        -- D2: solo un telefono con huella de prueba (un contacto en una
        -- cohorte, de hoy o de antes).
        'sin_huella_de_prueba', case when exists (
                select 1 from public.flow_cohort_members m where m.contact_id = any(v_contacts))
             or exists (select 1 from public.pilot_cohort_memberships m where m.contact_id = any(v_contacts))
             then 0 else 1 end,
        'nada_que_reciclar', case when jsonb_array_length(v_targets) = 0 then 1 else 0 end);

    return jsonb_build_object(
        'tenant_ref', p_tenant_ref, 'ally_ref', p_ally_ref, 'canonical', v_canonical, 'forms', to_jsonb(v_forms),
        'national', v_national, 'last2', right(v_canonical, 2), 'accounts', to_jsonb(v_accounts),
        'funnels', to_jsonb(v_funnels), 'contacts', to_jsonb(v_contacts), 'emails', to_jsonb(v_emails),
        'emails_exclusive', to_jsonb(v_exclusive), 'emails_shared', to_jsonb(v_shared),
        'emails_recycled', to_jsonb(v_recycled), 'new_email', v_new_email, 'recycle_emails', p_recycle_emails,
        'targets', v_targets, 'guards', v_guards,
        -- La huella de lo que vio el aprobador: los contactos, cada celda con
        -- su valor y las guardas. Lleva ids aleatorios, asi que no sirve para
        -- adivinar el telefono.
        'fingerprint', encode(sha256(convert_to(jsonb_build_object(
            'canonical', v_canonical, 'new_email', v_new_email, 'recycle_emails', p_recycle_emails,
            'contacts', to_jsonb(v_contacts), 'targets', v_targets, 'guards', v_guards,
            'emails_shared', to_jsonb(v_shared))::text, 'UTF8')), 'hex'));
end;
$function$;

-- La primera guarda que frena, en un orden fijo; null si ninguna.
create function public._identity_recycle_blocked_by(p_guards jsonb)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select g from unnest(array['nada_que_reciclar', 'sin_huella_de_prueba', 'fila_de_otra_persona',
                               'otro_formato_o_ruta', 'casos_abiertos', 'envios_pendientes',
                               'intenciones_vivas', 'mail_nuevo_ya_conocido']) with ordinality u(g, n)
     where coalesce((p_guards ->> g)::bigint, 0) <> 0
     order by n limit 1
$function$;

-- Los frenos de E-253 para una persona nueva con el telefono (y con cada mail
-- dado): la consulta de cada uno, sobre las filas de ahora. Despues de
-- reciclar, todos en 0 / false. Cada verificacion deja su fila: se cuentan
-- por presencia.
create function public._identity_recycle_checks(p_inventory jsonb, p_emails text[])
returns table (check_name text, value text, ok boolean)
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_forms text[] := array(select jsonb_array_elements_text(p_inventory -> 'forms'));
    v_accounts text[] := array(select jsonb_array_elements_text(p_inventory -> 'accounts'));
    v_funnels text[] := array(select jsonb_array_elements_text(p_inventory -> 'funnels'));
    v_tenant text := p_inventory ->> 'tenant_ref';
    v_emails text[] := coalesce(p_emails, array[]::text[]);
    v_mail text;
    v_funnel text;
    v_form text;
    v_n bigint;
    v_link boolean := false;
begin
    -- Las filas por tabla y el numero en otro formato: el inventario de
    -- ahora, con el mismo telefono.
    for check_name, v_n in
        select 'V_' || x.t, (select count(*) from jsonb_array_elements(p_inventory -> 'targets') y
                              where y ->> 'table' = x.t)
          from unnest(array['contact_points', 'contacts', 'channel_identities', 'purchase_intents',
                            'hotmart_purchase_intent_event_identities', 'webhook_events',
                            'contact_opt_out_events']) x(t)
    loop
        value := v_n::text; ok := v_n = 0; return next;
    end loop;
    check_name := 'V_otro_formato_o_ruta';
    value := p_inventory -> 'guards' ->> 'otro_formato_o_ruta';
    ok := value = '0'; return next;

    -- F1 _find_portable_precheckout_contact: duenos del telefono o del mail.
    select count(*) into v_n from (
        select cp.contact_id from public.contact_points cp where cp.type = 'email' and cp.normalized_value = any(v_emails)
        union select cp.contact_id from public.contact_points cp where cp.type = 'phone' and cp.normalized_value = any(v_forms)
        union select ci.contact_id from public.channel_identities ci
               where ci.channel = 'whatsapp' and ci.account_id = any(v_accounts) and ci.external_user_id = any(v_forms)
        union select c.id from public.contacts c where lower(c.email) = any(v_emails)
    ) o;
    check_name := 'F1_duenos_del_formulario'; value := v_n::text; ok := v_n = 0; return next;
    -- F2 find_contact_by_phones del bridge.
    select count(*) into v_n from public.contact_points cp where cp.type = 'phone' and cp.normalized_value = any(v_forms);
    check_name := 'F2_contacto_por_telefono'; value := v_n::text; ok := v_n = 0; return next;
    -- F3 la identidad de WhatsApp.
    select count(*) into v_n from public.channel_identities ci where ci.external_user_id = any(v_forms);
    check_name := 'F3_identidad_whatsapp'; value := v_n::text; ok := v_n = 0; return next;
    -- F4 la compra admitida por identidad (_portable_case_purchase_stop_reason y
    -- purchase_by_identity de _portable_precheckout_stop_reason).
    select count(*) into v_n from public.portable_hotmart_purchase_correlations pc
      join public.hotmart_purchase_intent_event_identities b on b.webhook_event_id = pc.webhook_event_id
     where pc.tenant_ref = v_tenant and pc.funnel_ref = any(v_funnels)
       and (b.normalized_email = any(v_emails) or b.normalized_phone = any(v_forms));
    check_name := 'F4_compra_admitida_por_identidad'; value := v_n::text; ok := v_n = 0; return next;
    -- F5 una intencion purchased de la misma persona.
    select count(*) into v_n from public.purchase_intents i
     where i.tenant_ref = v_tenant and i.funnel_ref = any(v_funnels) and i.lifecycle_state = 'purchased'
       and (i.normalized_email = any(v_emails) or i.normalized_phone = any(v_forms));
    check_name := 'F5_intencion_comprada'; value := v_n::text; ok := v_n = 0; return next;
    -- F6 una intencion viva con el telefono o el mail (identity_conflict).
    select count(*) into v_n from public.purchase_intents i
     where i.lifecycle_state = 'waiting_for_purchase'
       and (i.normalized_phone = any(v_forms) or i.normalized_email = any(v_emails));
    check_name := 'F6_intencion_viva'; value := v_n::text; ok := v_n = 0; return next;
    -- F7 _portable_checkout_link_purchase_exists, llamada de verdad, por cada
    -- producto de la instancia y cada mail (y sin mail).
    foreach v_funnel in array v_funnels loop
        v_link := v_link or public._portable_checkout_link_purchase_exists(
            v_tenant, v_funnel, '00000000-0000-0000-0000-000000000000'::uuid, null, p_inventory ->> 'canonical');
        foreach v_mail in array v_emails loop
            v_link := v_link or public._portable_checkout_link_purchase_exists(
                v_tenant, v_funnel, '00000000-0000-0000-0000-000000000000'::uuid, v_mail,
                p_inventory ->> 'canonical');
        end loop;
    end loop;
    check_name := 'F7_compra_con_nuestro_link'; value := v_link::text; ok := not v_link; return next;
    -- F8 la baja de Chatwoot del movil.
    select count(*) into v_n from public.contact_opt_out_events o
     where o.source = 'chatwoot' and o.channel = 'whatsapp' and o.external_user_id = any(v_forms)
       and o.correlation_status in ('applied', 'unmatched', 'ambiguous', 'evidence_conflict');
    check_name := 'F8_baja_del_movil'; value := v_n::text; ok := v_n = 0; return next;
    -- F9 get_portable_person_activity_v1, llamada de verdad, por cada producto
    -- y cada forma del telefono.
    v_n := 0;
    foreach v_funnel in array v_funnels loop
        foreach v_form in array v_forms loop
            v_n := v_n + (select count(*) from public.get_portable_person_activity_v1(v_tenant, v_funnel, v_form));
        end loop;
    end loop;
    check_name := 'F9_actividad_que_ve_el_agente'; value := v_n::text; ok := v_n = 0; return next;
end;
$function$;

-- Un mail enmascarado: d···@g···.com.
create function public._identity_recycle_mask_email(p_email text)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select left(split_part(p_email, '@', 1), 1) || '···@' || left(split_part(p_email, '@', 2), 1) || '···'
           || coalesce('.' || nullif(regexp_replace(split_part(p_email, '@', 2), '^.*\.', ''), split_part(p_email, '@', 2)), '')
$function$;

-- La vista para el aprobador, en lenguaje de negocio y sin identidad: los
-- ultimos dos digitos, los conteos, las cohortes, las bajas con su fecha, los
-- mails enmascarados, las guardas y la huella.
create function public._identity_recycle_view(p_inventory jsonb)
returns jsonb
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_contacts uuid[] := array(select (jsonb_array_elements_text(p_inventory -> 'contacts'))::uuid);
    v_targets jsonb := p_inventory -> 'targets';
    v_rows jsonb;
    v_blocked text := public._identity_recycle_blocked_by(p_inventory -> 'guards');
begin
    select coalesce(jsonb_object_agg(t, n), '{}'::jsonb) into v_rows from (
        select x ->> 'table' t, count(distinct x ->> 'row_id') n from jsonb_array_elements(v_targets) x group by 1
    ) y;
    return jsonb_build_object(
        'ok', true,
        'last2', p_inventory ->> 'last2',
        'can_recycle', v_blocked is null,
        'blocked_by', v_blocked,
        'guards', p_inventory -> 'guards',
        'people', jsonb_build_object(
            'contacts', cardinality(v_contacts),
            'opted_out', (select count(*) from public.contacts c
                           where c.id = any(v_contacts) and c.contact_permission = 'opted_out')),
        'cohorts', coalesce((
            select jsonb_agg(jsonb_build_object('kind', k, 'name', n, 'since', s) order by s, n)
              from (
                  select 'flujo' k, fc.product_ref || '/' || fc.flow_key n, min(m.added_at) s
                    from public.flow_cohort_members m join public.flow_controls fc on fc.flow_id = m.flow_id
                   where m.contact_id = any(v_contacts) group by 1, 2
                  union all
                  select 'piloto', m.scope_key, min(m.enrolled_at)
                    from public.pilot_cohort_memberships m
                   where m.contact_id = any(v_contacts) group by 1, 2
              ) c), '[]'::jsonb),
        'history', jsonb_build_object(
            'purchases', (select count(distinct w.id) from public.webhook_events w
                           where w.event_type = 'PURCHASE_APPROVED'
                             and w.id in (select (x ->> 'row_id')::uuid from jsonb_array_elements(v_targets) x
                                           where x ->> 'table' = 'webhook_events')),
            'carts', (select count(distinct w.id) from public.webhook_events w
                       where w.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
                         and w.id in (select (x ->> 'row_id')::uuid from jsonb_array_elements(v_targets) x
                                       where x ->> 'table' = 'webhook_events')),
            'other_events', (select count(distinct w.id) from public.webhook_events w
                              where w.event_type not in ('PURCHASE_APPROVED', 'PURCHASE_OUT_OF_SHOPPING_CART')
                                and w.id in (select (x ->> 'row_id')::uuid from jsonb_array_elements(v_targets) x
                                              where x ->> 'table' = 'webhook_events')),
            'forms', coalesce((v_rows ->> 'purchase_intents')::bigint, 0),
            'cases', (select count(*) from public.recovery_cases rc where rc.contact_id = any(v_contacts)),
            'runs', (select count(*) from public.flow_engine_runs r where r.person_ref = any(v_contacts))),
        'opt_outs', coalesce((
            select jsonb_agg(to_char(o.occurred_at at time zone 'UTC', 'YYYY-MM-DD') order by o.occurred_at)
              from public.contact_opt_out_events o
             where o.id in (select (x ->> 'row_id')::uuid from jsonb_array_elements(v_targets) x
                             where x ->> 'table' = 'contact_opt_out_events')), '[]'::jsonb),
        'rows', v_rows,
        'cells', jsonb_array_length(v_targets),
        'emails', jsonb_build_object(
            'recycle', (p_inventory ->> 'recycle_emails')::boolean,
            'recycled', coalesce((select jsonb_agg(public._identity_recycle_mask_email(e) order by e)
                                    from jsonb_array_elements_text(p_inventory -> 'emails_recycled') e), '[]'::jsonb),
            'shared', coalesce((select jsonb_agg(public._identity_recycle_mask_email(e) order by e)
                                  from jsonb_array_elements_text(p_inventory -> 'emails_shared') e), '[]'::jsonb),
            'not_recycled', coalesce((select jsonb_agg(public._identity_recycle_mask_email(e) order by e)
                                        from jsonb_array_elements_text(p_inventory -> 'emails_exclusive') e
                                       where not (p_inventory ->> 'recycle_emails')::boolean), '[]'::jsonb)),
        'fingerprint', p_inventory ->> 'fingerprint');
end;
$function$;

-- ===========================================================================
-- 4. Aplicar, deshacer y purgar (internas).
-- ===========================================================================

-- Reescribe las celdas del diario de un reciclado en su tabla, del valor de
-- partida al de llegada: el marcador al aplicar, el valor viejo al deshacer.
-- Las dos tablas inmutables lo dejan pasar por la excepcion. Devuelve cuantas
-- filas cambio por tabla.
create function public._identity_recycle_write(p_recycle_id uuid, p_forward boolean)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_out jsonb := '{}'::jsonb;
    v_n bigint;
begin
    with j as (
        select row_id, jsonb_object_agg(column_name, case when p_forward then marker else old_value end) v
          from public.identity_recycle_rows where recycle_id = p_recycle_id and table_name = 'contact_points'
         group by row_id
    ), u as (
        update public.contact_points x
           set normalized_value = coalesce(j.v ->> 'normalized_value', x.normalized_value),
               raw_value = coalesce(j.v ->> 'raw_value', x.raw_value)
          from j where x.id = j.row_id returning 1)
    select count(*) into v_n from u;
    v_out := v_out || jsonb_build_object('contact_points', v_n);

    with j as (
        select row_id, jsonb_object_agg(column_name, case when p_forward then marker else old_value end) v
          from public.identity_recycle_rows where recycle_id = p_recycle_id and table_name = 'contacts'
         group by row_id
    ), u as (
        update public.contacts x
           set phone = case when j.v ? 'phone' then j.v ->> 'phone' else x.phone end,
               email = case when j.v ? 'email' then j.v ->> 'email' else x.email end
          from j where x.id = j.row_id returning 1)
    select count(*) into v_n from u;
    v_out := v_out || jsonb_build_object('contacts', v_n);

    with j as (
        select row_id, jsonb_object_agg(column_name, case when p_forward then marker else old_value end) v
          from public.identity_recycle_rows where recycle_id = p_recycle_id and table_name = 'channel_identities'
         group by row_id
    ), u as (
        update public.channel_identities x set external_user_id = j.v ->> 'external_user_id'
          from j where x.id = j.row_id returning 1)
    select count(*) into v_n from u;
    v_out := v_out || jsonb_build_object('channel_identities', v_n);

    with j as (
        select row_id, jsonb_object_agg(column_name, case when p_forward then marker else old_value end) v
          from public.identity_recycle_rows where recycle_id = p_recycle_id and table_name = 'purchase_intents'
         group by row_id
    ), u as (
        update public.purchase_intents x
           set normalized_phone = case when j.v ? 'normalized_phone' then j.v ->> 'normalized_phone'
                                       else x.normalized_phone end,
               normalized_email = case when j.v ? 'normalized_email' then j.v ->> 'normalized_email'
                                       else x.normalized_email end
          from j where x.id = j.row_id returning 1)
    select count(*) into v_n from u;
    v_out := v_out || jsonb_build_object('purchase_intents', v_n);

    with j as (
        select row_id, jsonb_object_agg(column_name, case when p_forward then marker else old_value end) v
          from public.identity_recycle_rows
         where recycle_id = p_recycle_id and table_name = 'hotmart_purchase_intent_event_identities'
         group by row_id
    ), u as (
        update public.hotmart_purchase_intent_event_identities x
           set normalized_phone = case when j.v ? 'normalized_phone' then j.v ->> 'normalized_phone'
                                       else x.normalized_phone end,
               normalized_email = case when j.v ? 'normalized_email' then j.v ->> 'normalized_email'
                                       else x.normalized_email end
          from j where x.webhook_event_id = j.row_id returning 1)
    select count(*) into v_n from u;
    v_out := v_out || jsonb_build_object('hotmart_purchase_intent_event_identities', v_n);

    with j as (
        select row_id, jsonb_object_agg(column_name, case when p_forward then marker else old_value end) v
          from public.identity_recycle_rows where recycle_id = p_recycle_id and table_name = 'webhook_events'
         group by row_id
    ), u as (
        update public.webhook_events x
           set payload = public._identity_recycle_set_paths(x.payload, j.v)
          from j where x.id = j.row_id returning 1)
    select count(*) into v_n from u;
    v_out := v_out || jsonb_build_object('webhook_events', v_n);

    with j as (
        select row_id, jsonb_object_agg(column_name, case when p_forward then marker else old_value end) v
          from public.identity_recycle_rows where recycle_id = p_recycle_id and table_name = 'contact_opt_out_events'
         group by row_id
    ), u as (
        update public.contact_opt_out_events x set external_user_id = j.v ->> 'external_user_id'
          from j where x.id = j.row_id returning 1)
    select count(*) into v_n from u;
    v_out := v_out || jsonb_build_object('contact_opt_out_events', v_n);
    return v_out;
end;
$function$;

-- Si el telefono (o un mail reciclado) volvio a entrar despues del
-- reciclado: alguna fila de las tablas del inventario lo tiene. Las celdas
-- del diario tienen el marcador, asi que cualquier coincidencia es nueva.
create function public._identity_recycle_returned(p_recycle_id uuid)
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    with phones as (
        select distinct v from public.identity_recycle_rows j
         cross join lateral unnest(public._whatsapp_phone_variants(
             public._whatsapp_phone_canonical(nullif(regexp_replace(j.old_value, '\D', '', 'g'), '')))) v
         where j.recycle_id = p_recycle_id and j.kind = 'phone' and j.old_value is not null
    ), mails as (
        select distinct lower(btrim(j.old_value)) v from public.identity_recycle_rows j
         where j.recycle_id = p_recycle_id and j.kind = 'email' and j.old_value is not null
    )
    select exists (select 1 from public.contact_points cp
                    where (cp.type = 'phone' and regexp_replace(cp.normalized_value, '\D', '', 'g') in (select v from phones))
                       or (cp.type = 'email' and lower(btrim(cp.normalized_value)) in (select v from mails)))
        or exists (select 1 from public.contacts c
                    where regexp_replace(coalesce(c.phone, ''), '\D', '', 'g') in (select v from phones)
                       or lower(btrim(c.email)) in (select v from mails))
        or exists (select 1 from public.channel_identities ci
                    where regexp_replace(coalesce(ci.external_user_id, ''), '\D', '', 'g') in (select v from phones))
        or exists (select 1 from public.purchase_intents i
                    where i.normalized_phone in (select v from phones) or i.normalized_email in (select v from mails))
        or exists (select 1 from public.hotmart_purchase_intent_event_identities h
                    where h.normalized_phone in (select v from phones) or h.normalized_email in (select v from mails))
        or exists (select 1 from public.contact_opt_out_events o where o.external_user_id in (select v from phones))
        or exists (select 1 from public.webhook_events w
                    where regexp_replace(coalesce(w.payload #>> '{data,buyer,phone}', ''), '\D', '', 'g')
                              in (select v from phones)
                       or regexp_replace(coalesce(w.payload #>> '{data,buyer,checkout_phone}', ''), '\D', '', 'g')
                              in (select v from phones)
                       or lower(btrim(w.payload #>> '{data,buyer,email}')) in (select v from mails))
$function$;

create function public._identity_recycle_apply(
    p_tenant_ref text, p_ally_ref text, p_actor_kind text, p_actor_ref text, p_phone text, p_new_email text,
    p_recycle_emails boolean, p_fingerprint text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_inventory jsonb;
    v_after jsonb;
    v_blocked text;
    v_recycle public.identity_recycles%rowtype;
    v_id8 text;
    v_digits text;
    v_written jsonb;
    v_expected jsonb;
    v_contacts uuid[];
    v_failed text[];
    v_checks integer;
    v_counts jsonb;
begin
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'identity_recycle_fingerprint_invalid';
    end if;
    if not coalesce(public._flow_store_actor_is_valid(p_actor_kind, p_actor_ref), false) then
        raise exception using errcode = '22023', message = 'identity_recycle_actor_invalid';
    end if;
    -- Un reciclado por instancia a la vez: dos aplicaciones del mismo
    -- telefono no se cruzan.
    perform pg_advisory_xact_lock(hashtextextended('identity_recycle:' || p_tenant_ref || ':' || p_ally_ref, 0));

    v_inventory := public._identity_recycle_inventory(p_tenant_ref, p_ally_ref, p_phone, p_new_email, p_recycle_emails);
    v_blocked := public._identity_recycle_blocked_by(v_inventory -> 'guards');
    if v_blocked is not null then
        return jsonb_build_object('ok', false, 'reason_code', v_blocked, 'guards', v_inventory -> 'guards');
    end if;
    if v_inventory ->> 'fingerprint' <> p_fingerprint then
        return jsonb_build_object('ok', false, 'reason_code', 'vista_vencida');
    end if;
    v_contacts := array(select (jsonb_array_elements_text(v_inventory -> 'contacts'))::uuid);

    insert into public.identity_recycles (tenant_ref, ally_ref, actor_kind, actor_ref, status, txid, fingerprint,
                                          phone_last2, recycle_emails)
    values (p_tenant_ref, p_ally_ref, p_actor_kind, p_actor_ref, 'aplicando', txid_current(), p_fingerprint,
            v_inventory ->> 'last2', p_recycle_emails)
    returning * into v_recycle;
    v_id8 := left(v_recycle.id::text, 8);
    v_digits := '999' || lpad(v_recycle.recycle_number::text, 9, '0');

    -- El diario, celda por celda, con su marcador.
    insert into public.identity_recycle_rows (recycle_id, table_name, row_id, column_name, kind, old_value, marker)
    select v_recycle.id, x ->> 'table', (x ->> 'row_id')::uuid, x ->> 'column', x ->> 'kind', x ->> 'old_value',
           case when x ->> 'kind' = 'email'
                then 'reciclado-' || v_recycle.recycle_number || '-'
                     || array_position(array(select jsonb_array_elements_text(v_inventory -> 'emails_recycled')),
                                       lower(btrim(x ->> 'old_value')))
                     || '@reciclado.invalid'
                when x ->> 'table' in ('contact_points', 'contacts', 'channel_identities')
                then 'reciclado:' || v_id8 || ':' || left(x ->> 'row_id', 8)
                else v_digits end
      from jsonb_array_elements(v_inventory -> 'targets') x;

    v_written := public._identity_recycle_write(v_recycle.id, true);
    select coalesce(jsonb_object_agg(t, n), '{}'::jsonb) into v_expected from (
        select table_name t, count(distinct row_id) n from public.identity_recycle_rows
         where recycle_id = v_recycle.id group by 1) y;
    if exists (select 1 from jsonb_each_text(v_expected) e
                where coalesce((v_written ->> e.key)::bigint, -1) <> e.value::bigint) then
        raise exception using errcode = '55000', message = 'identity_recycle_write_mismatch',
            detail = v_written::text || ' ' || v_expected::text;
    end if;

    -- Las verificaciones, por presencia, antes del COMMIT: el inventario de
    -- ahora con el mismo telefono y los frenos para una persona nueva, con el
    -- mail nuevo y con los mails reciclados.
    v_after := public._identity_recycle_inventory(p_tenant_ref, p_ally_ref, p_phone, null, p_recycle_emails);
    v_after := jsonb_set(v_after, '{guards,otro_formato_o_ruta}', to_jsonb(public._identity_recycle_residue(
        array[v_inventory ->> 'national']
        || array(select jsonb_array_elements_text(v_inventory -> 'emails_recycled')), '[]'::jsonb)));
    select array_agg(c.check_name order by c.check_name) filter (where not c.ok), count(*) filter (where c.ok)
      into v_failed, v_checks
      from public._identity_recycle_checks(v_after,
          array_remove(array[v_inventory ->> 'new_email'], null)
          || array(select jsonb_array_elements_text(v_inventory -> 'emails_recycled'))) c;
    if v_failed is not null or v_checks <> 17 then
        raise exception using errcode = '55000', message = 'identity_recycle_verification_failed',
            detail = coalesce(array_to_string(v_failed, ','), '') || ' ok=' || v_checks;
    end if;

    -- Lo que queda registrado como prueba: lo reescrito y lo que cuelga del
    -- contacto viejo (casos, sus casos comerciales, emisiones, eventos e
    -- intenciones correlacionadas). Sin identidad.
    insert into public.identity_recycle_activity (recycle_id, kind, ref_id)
    select distinct v_recycle.id, k, id from (
        select 'contact'::text k, c id from unnest(v_contacts) c
        union all
        select 'purchase_intent', j.row_id from public.identity_recycle_rows j
         where j.recycle_id = v_recycle.id and j.table_name = 'purchase_intents'
        union all
        select 'webhook_event', j.row_id from public.identity_recycle_rows j
         where j.recycle_id = v_recycle.id
           and j.table_name in ('webhook_events', 'hotmart_purchase_intent_event_identities')
        union all
        select 'recovery_case', rc.id from public.recovery_cases rc where rc.contact_id = any(v_contacts)
        union all
        select 'commercial_case', cc.id from public.commercial_cases cc where cc.contact_id = any(v_contacts)
        union all
        select 'checkout_link_issuance', li.id from public.checkout_link_issuances li
         where li.contact_id = any(v_contacts)
        union all
        select 'webhook_event', pc.webhook_event_id from public.portable_hotmart_purchase_correlations pc
         where pc.purchase_intent_id in (select j.row_id from public.identity_recycle_rows j
                                          where j.recycle_id = v_recycle.id and j.table_name = 'purchase_intents')
        union all
        select 'webhook_event', hc.webhook_event_id from public.hotmart_purchase_intent_correlations hc
         where hc.purchase_intent_id in (select j.row_id from public.identity_recycle_rows j
                                          where j.recycle_id = v_recycle.id and j.table_name = 'purchase_intents')
        union all
        select 'webhook_event', li.hotmart_webhook_event_id from public.checkout_link_issuances li
         where li.contact_id = any(v_contacts) and li.hotmart_webhook_event_id is not null
    ) a;

    v_counts := jsonb_build_object('rows', v_expected, 'cells',
        (select count(*) from public.identity_recycle_rows where recycle_id = v_recycle.id),
        'contacts', cardinality(v_contacts), 'checks', v_checks,
        'test_activity', (select count(*) from public.identity_recycle_activity where recycle_id = v_recycle.id));
    update public.identity_recycles set status = 'aplicado', applied_at = clock_timestamp(), counts = v_counts
     where id = v_recycle.id;
    return jsonb_build_object('ok', true, 'recycle_id', v_recycle.id, 'recycle_number', v_recycle.recycle_number,
        'last2', v_recycle.phone_last2, 'counts', v_counts);
end;
$function$;

-- Por que un reciclado no se puede deshacer; null si se puede. D5: 72 h, o
-- hasta que el telefono vuelve a entrar, lo que pase primero.
create function public._identity_recycle_undo_blocked(p_recycle public.identity_recycles)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when p_recycle.status = 'purgado' or p_recycle.purged_at is not null then 'identity_recycle_purged'
        when p_recycle.status = 'deshecho' then 'identity_recycle_already_undone'
        when p_recycle.status <> 'aplicado' then 'identity_recycle_not_applied'
        when p_recycle.applied_at <= clock_timestamp() - interval '72 hours' then 'identity_recycle_undo_window_closed'
        when exists (select 1 from public.identity_recycle_rows j
                      where j.recycle_id = p_recycle.id and j.old_value is null) then 'identity_recycle_purged'
        when public._identity_recycle_returned(p_recycle.id) then 'identity_recycle_phone_returned'
    end
$function$;

create function public._identity_recycle_undo(
    p_tenant_ref text, p_ally_ref text, p_actor_kind text, p_actor_ref text, p_recycle_id uuid)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_recycle public.identity_recycles%rowtype;
    v_blocked text;
    v_written jsonb;
    v_expected jsonb;
begin
    if not coalesce(public._flow_store_actor_is_valid(p_actor_kind, p_actor_ref), false) then
        raise exception using errcode = '22023', message = 'identity_recycle_actor_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended('identity_recycle:' || p_tenant_ref || ':' || p_ally_ref, 0));
    select * into v_recycle from public.identity_recycles r
     where r.id = p_recycle_id and r.tenant_ref = p_tenant_ref and r.ally_ref = p_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'identity_recycle_not_found';
    end if;
    v_blocked := public._identity_recycle_undo_blocked(v_recycle);
    if v_blocked is not null then
        return jsonb_build_object('ok', false, 'reason_code', v_blocked);
    end if;
    -- Cada celda tiene que tener todavia su marcador: si algo la cambio
    -- despues, deshacer pisaria un dato que no es del reciclado.
    if exists (
        select 1 from public.identity_recycle_rows j
         where j.recycle_id = v_recycle.id
           and j.marker is distinct from case
               when j.table_name = 'contact_points' then
                   (select case j.column_name when 'normalized_value' then x.normalized_value else x.raw_value end
                      from public.contact_points x where x.id = j.row_id)
               when j.table_name = 'contacts' then
                   (select case j.column_name when 'phone' then x.phone else x.email end
                      from public.contacts x where x.id = j.row_id)
               when j.table_name = 'channel_identities' then
                   (select x.external_user_id from public.channel_identities x where x.id = j.row_id)
               when j.table_name = 'purchase_intents' then
                   (select case j.column_name when 'normalized_phone' then x.normalized_phone else x.normalized_email end
                      from public.purchase_intents x where x.id = j.row_id)
               when j.table_name = 'hotmart_purchase_intent_event_identities' then
                   (select case j.column_name when 'normalized_phone' then x.normalized_phone else x.normalized_email end
                      from public.hotmart_purchase_intent_event_identities x where x.webhook_event_id = j.row_id)
               when j.table_name = 'webhook_events' then
                   (select x.payload #>> string_to_array(substr(j.column_name, 9), '.')
                      from public.webhook_events x where x.id = j.row_id)
               when j.table_name = 'contact_opt_out_events' then
                   (select x.external_user_id from public.contact_opt_out_events x where x.id = j.row_id)
           end
    ) then
        return jsonb_build_object('ok', false, 'reason_code', 'identity_recycle_changed_since');
    end if;

    update public.identity_recycles set status = 'deshaciendo', txid = txid_current() where id = v_recycle.id;
    v_written := public._identity_recycle_write(v_recycle.id, false);
    select coalesce(jsonb_object_agg(t, n), '{}'::jsonb) into v_expected from (
        select table_name t, count(distinct row_id) n from public.identity_recycle_rows
         where recycle_id = v_recycle.id group by 1) y;
    if exists (select 1 from jsonb_each_text(v_expected) e
                where coalesce((v_written ->> e.key)::bigint, -1) <> e.value::bigint) then
        raise exception using errcode = '55000', message = 'identity_recycle_write_mismatch',
            detail = v_written::text || ' ' || v_expected::text;
    end if;
    update public.identity_recycles
       set status = 'deshecho', undone_at = clock_timestamp(), undone_by_kind = p_actor_kind,
           undone_by_ref = p_actor_ref
     where id = v_recycle.id;
    -- Los valores viejos ya volvieron a su tabla: el diario no guarda una
    -- copia de mas.
    update public.identity_recycle_rows set old_value = null where recycle_id = v_recycle.id;
    update public.identity_recycles set purged_at = clock_timestamp() where id = v_recycle.id;
    return jsonb_build_object('ok', true, 'recycle_id', v_recycle.id, 'status', 'deshecho', 'rows', v_written);
end;
$function$;

-- La lectura de un reciclado, sin identidad.
create function public._identity_recycle_read(p_recycle public.identity_recycles)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object('ok', true, 'recycle_id', p_recycle.id, 'recycle_number', p_recycle.recycle_number,
        'status', p_recycle.status, 'last2', p_recycle.phone_last2, 'recycle_emails', p_recycle.recycle_emails,
        'counts', p_recycle.counts, 'actor_kind', p_recycle.actor_kind, 'actor_ref', p_recycle.actor_ref,
        'created_at', p_recycle.created_at, 'applied_at', p_recycle.applied_at,
        'undone_at', p_recycle.undone_at, 'purged_at', p_recycle.purged_at,
        'can_undo', public._identity_recycle_undo_blocked(p_recycle) is null,
        'undo_blocked_by', public._identity_recycle_undo_blocked(p_recycle),
        'undo_until', p_recycle.applied_at + interval '72 hours')
$function$;

-- ===========================================================================
-- 5. Los envoltorios del aprobador, con la sesion viva.
-- ===========================================================================

-- La vista previa: de solo lectura (salvo la marca de uso de la sesion). El
-- telefono entra y no vuelve: la respuesta trae los ultimos dos digitos.
create function public.approver_identity_recycle_preview_v1(
    p_instance jsonb, p_session_hash text, p_phone text, p_new_email text, p_recycle_emails boolean)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public._identity_recycle_view(public._identity_recycle_inventory(
        v_instance.o_tenant_ref, v_instance.o_ally_ref, p_phone, p_new_email, p_recycle_emails));
end;
$function$;

-- Aplicar lo que vio el aprobador: la huella de la vista viaja con el si
-- (D8: siempre con segundo paso; el token de la confirmacion es del bridge).
create function public.approver_identity_recycle_apply_v1(
    p_instance jsonb, p_session_hash text, p_phone text, p_new_email text, p_recycle_emails boolean,
    p_fingerprint text)
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
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public._identity_recycle_apply(v_instance.o_tenant_ref, v_instance.o_ally_ref, 'slack', v_actor,
        p_phone, p_new_email, p_recycle_emails, p_fingerprint);
end;
$function$;

create function public.approver_get_identity_recycle_v1(
    p_instance jsonb, p_session_hash text, p_recycle_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_recycle public.identity_recycles%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_recycle from public.identity_recycles r
     where r.id = p_recycle_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'identity_recycle_not_found';
    end if;
    return public._identity_recycle_read(v_recycle);
end;
$function$;

create function public.approver_identity_recycle_undo_v1(
    p_instance jsonb, p_session_hash text, p_recycle_id uuid)
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
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public._identity_recycle_undo(v_instance.o_tenant_ref, v_instance.o_ally_ref, 'slack', v_actor,
        p_recycle_id);
end;
$function$;

-- ===========================================================================
-- 6. La purga (service_role, el tick del bridge) y las ids de prueba.
-- ===========================================================================

create function public.purge_identity_recycles_v1()
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_recycle public.identity_recycles%rowtype;
    v_purged integer := 0;
begin
    for v_recycle in
        select * from public.identity_recycles r
         where r.status = 'aplicado'
         order by r.applied_at
         for update skip locked
    loop
        if v_recycle.applied_at <= clock_timestamp() - interval '72 hours'
           or public._identity_recycle_returned(v_recycle.id) then
            update public.identity_recycle_rows set old_value = null
             where recycle_id = v_recycle.id and old_value is not null;
            update public.identity_recycles set status = 'purgado', purged_at = clock_timestamp()
             where id = v_recycle.id;
            v_purged := v_purged + 1;
        end if;
    end loop;
    return jsonb_build_object('ok', true, 'purged', v_purged);
end;
$function$;

-- Las ids que dejaron las pruebas recicladas, por tipo, sin identidad. Un
-- reciclado deshecho no cuenta: su telefono volvio a ser el de antes.
create function public.test_activity_ids_v1()
returns table (kind text, id uuid)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select distinct a.kind, a.ref_id
      from public.identity_recycle_activity a
      join public.identity_recycles r on r.id = a.recycle_id
     where r.status in ('aplicado', 'purgado')
     order by 1, 2
$function$;

-- ===========================================================================
-- 7. Los permisos. Las tablas, para nadie de la API. Las internas y los
--    disparadores, para nadie. Las del aprobador, solo para su rol; la purga,
--    solo para service_role; test_activity_ids_v1, para nadie de la API (la
--    leen los SQL del OS como duenio). Nunca el rol del Automation Expert.
-- ===========================================================================

do $acl$
declare
    v_table text;
    v_function regprocedure;
    v_role text;
begin
    foreach v_table in array array['identity_recycles', 'identity_recycle_rows', 'identity_recycle_activity'] loop
        execute format('revoke all on table public.%I from public', v_table);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role', 'automation_expert_change',
                                 'flow_change_approver')
             order by r.rolname
        loop
            execute format('revoke all on table public.%I from %I', v_table, v_role);
        end loop;
    end loop;
    foreach v_function in array array[
        'public.guard_identity_recycle_change()',
        'public.guard_identity_recycle_journal_change()',
        'public._identity_recycle_allows(text,jsonb,jsonb)',
        'public._identity_recycle_contacts(text[])',
        'public._identity_recycle_targets(text[],text[],uuid[],text[],text[],text[])',
        'public._identity_recycle_set_paths(jsonb,jsonb)',
        'public._identity_recycle_residue(text[],jsonb)',
        'public._identity_recycle_inventory(text,text,text,text,boolean)',
        'public._identity_recycle_blocked_by(jsonb)',
        'public._identity_recycle_checks(jsonb,text[])',
        'public._identity_recycle_mask_email(text)',
        'public._identity_recycle_view(jsonb)',
        'public._identity_recycle_write(uuid,boolean)',
        'public._identity_recycle_returned(uuid)',
        'public._identity_recycle_apply(text,text,text,text,text,text,boolean,text)',
        'public._identity_recycle_undo_blocked(public.identity_recycles)',
        'public._identity_recycle_undo(text,text,text,text,uuid)',
        'public._identity_recycle_read(public.identity_recycles)',
        'public.approver_identity_recycle_preview_v1(jsonb,text,text,text,boolean)',
        'public.approver_identity_recycle_apply_v1(jsonb,text,text,text,boolean,text)',
        'public.approver_get_identity_recycle_v1(jsonb,text,uuid)',
        'public.approver_identity_recycle_undo_v1(jsonb,text,uuid)',
        'public.purge_identity_recycles_v1()',
        'public.test_activity_ids_v1()'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role', 'automation_expert_change',
                                 'flow_change_approver')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    -- El rol de la pagina lo crea 20261028000100; una base sin el no lo nombra.
    if to_regrole('flow_change_approver') is not null then
        foreach v_function in array array[
            'public.approver_identity_recycle_preview_v1(jsonb,text,text,text,boolean)',
            'public.approver_identity_recycle_apply_v1(jsonb,text,text,text,boolean,text)',
            'public.approver_get_identity_recycle_v1(jsonb,text,uuid)',
            'public.approver_identity_recycle_undo_v1(jsonb,text,uuid)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end loop;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.purge_identity_recycles_v1() to service_role;
    end if;
end;
$acl$;

commit;
