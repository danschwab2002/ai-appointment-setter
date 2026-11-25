-- bulk-send-prepared-and-base-vetoes-v1: lo que le falta a la base del masivo
-- manejado por el Automation Expert (20261125000200, encargo 3) para preparar
-- (encargo 7, PR #505) y para los vetos de la base (encargo 4, PR #506).
--
-- Por que hace falta:
--   * Preparar (lo encontro la trabajadora del #505): bulk_send_recipients no sirve
--     para «preparado» (su estado solo admite reservado/enviado/fallido/dudoso/
--     vetado, reservar exige el envio en enviando y una fila ahi saca a la persona
--     de read_bulk_send_batch_v1, o sea que el motor ya no le mandaria), y
--     read_bulk_send_batch_v1 no tiene cursor: preparar no escribe en
--     bulk_send_recipients, asi que veria siempre a las mismas personas.
--   * Los vetos (el #506): contact_opt_out_events y las compras de Hotmart estan
--     cerradas para service_role y ninguna funcion devuelve bajas ni compras para
--     una lista; y bulk_sends no guarda el producto del envio, del que depende
--     «compro».
--
-- 1. Una tabla nueva, vacia:
--    * bulk_send_prepared: por envio y posicion, el contacto, la conversacion (el
--      numero de la cuenta, el id de la API), el source_id del contacto en la
--      bandeja (el telefono al que va la plantilla: el motor lo revalida contra
--      la lista antes de mandar), la etiqueta de la campana que se le puso y como
--      quedo (nuevo, conversacion_nueva o reusa). Lo anotado no se pisa.
-- 2. Una columna nueva: bulk_sends.product_refs, los productos del envio (los
--    funnel_ref de las compras), que fija el AE en el borrador. Nula en lo que ya
--    existe; create_bulk_send_v1 la acepta (product_refs, opcional) y
--    _bulk_send_view la muestra. Las dos se redefinen con la misma firma.
-- 3. Funciones del bridge, solo de service_role:
--    * record_bulk_send_prepared_v1, read_bulk_send_prepared_v1 y
--      read_bulk_send_members_v1: la interfaz BulkPrepareStore de
--      src/bridge/bulk_send_prepare.py (anotar, leer lo anotado y la lista con
--      cursor por posicion).
--    * read_bulk_list_base_vetoes_v1: los vetos de la base para una lista
--      (SupabaseBulkVetoStore.base_vetoes de src/bridge/bulk_send_vetoes.py):
--      baja (un evento de baja por el telefono en cualquiera de sus formas, o una
--      autorizacion de WhatsApp denegada vigente de un contacto con ese telefono)
--      y compro (una compra aprobada sin reembolso ni contracargo de uno de los
--      productos del envio, por el telefono o por la huella del mail del
--      comprador contra la huella de la lista). Los productos salen de
--      bulk_sends.product_refs; p_product_refs es opcional y, si viene, tiene que
--      ser el mismo conjunto.
-- 4. La purga (purge_bulk_list_phones_v1, misma firma y mismos permisos) borra
--    ademas lo anotado del envio de la lista purgada: el source_id es un telefono.
--
-- Aditiva salvo las tres redefiniciones (create or replace, misma firma, la ACL
-- no cambia). Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida, P0002 lo que no existe. Los resultados de estado
-- vuelven como {"ok": false, "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.bulk_sends') is null
       or to_regclass('public.bulk_list_members') is null
       or to_regprocedure('public.purge_bulk_list_phones_v1(timestamptz)') is null
       or to_regprocedure('public.create_bulk_send_v1(jsonb,jsonb)') is null
       or to_regprocedure('public._bulk_send_view(uuid)') is null
       or to_regprocedure('public._whatsapp_phone_variants(text)') is null
       or to_regclass('public.contact_opt_out_events') is null
       or to_regclass('public.contact_authorizations') is null
       or to_regclass('public.portable_hotmart_purchase_correlations') is null then
        raise exception using errcode = '55000', message = 'bulk_send_prepared_prerequisite_missing';
    end if;
    if to_regclass('public.bulk_send_prepared') is not null then
        raise exception using errcode = '55000', message = 'bulk_send_prepared_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. La tabla.
-- ===========================================================================

create table public.bulk_send_prepared (
    send_id uuid not null references public.bulk_sends (id) on delete restrict,
    list_id uuid not null,
    position integer not null,
    chatwoot_contact_id bigint not null check (chatwoot_contact_id > 0),
    chatwoot_conversation_id bigint not null check (chatwoot_conversation_id > 0),
    -- El wa_id del contacto en la bandeja (FirstTouchRecipient: solo digitos).
    chatwoot_source_id text not null check (chatwoot_source_id ~ '^[1-9][0-9]{6,14}$'),
    label text check (label is null or label ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    how text not null check (how in ('nuevo', 'conversacion_nueva', 'reusa')),
    prepared_at timestamptz not null default clock_timestamp(),
    primary key (send_id, position),
    foreign key (list_id, position) references public.bulk_list_members (list_id, position) on delete restrict
);

alter table public.bulk_send_prepared enable row level security;

-- ===========================================================================
-- 2. Las funciones del bridge (service_role).
-- ===========================================================================

-- Anotar una persona preparada: p_prepared = {"contact_id", "conversation_id", "source_id", "label", "how"}.
-- Lo ya anotado no se pisa: una segunda vez devuelve already_prepared con la conversacion que quedo.
create function public.record_bulk_send_prepared_v1(p_send_id uuid, p_position integer, p_prepared jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_send public.bulk_sends%rowtype;
    v_member public.bulk_list_members%rowtype;
    v_existing bigint;
begin
    if p_send_id is null or p_position is null or p_position < 0
       or jsonb_typeof(p_prepared) is distinct from 'object'
       or jsonb_typeof(p_prepared -> 'contact_id') is distinct from 'number'
       or jsonb_typeof(p_prepared -> 'conversation_id') is distinct from 'number'
       or jsonb_typeof(p_prepared -> 'source_id') is distinct from 'string'
       or jsonb_typeof(p_prepared -> 'how') is distinct from 'string'
       or coalesce(jsonb_typeof(p_prepared -> 'label'), 'null') not in ('string', 'null') then
        raise exception using errcode = '22023', message = 'bulk_send_prepared_invalid';
    end if;
    select * into v_send from public.bulk_sends where id = p_send_id for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_send_not_found';
    end if;
    if v_send.state <> 'preparando' then
        return jsonb_build_object('ok', false, 'reason_code', 'not_preparing', 'state', v_send.state);
    end if;
    if (p_prepared ->> 'label') is distinct from v_send.campaign_label then
        raise exception using errcode = '22023', message = 'bulk_send_prepared_label_mismatch';
    end if;
    select * into v_member from public.bulk_list_members where list_id = v_send.list_id and position = p_position;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_list_member_not_found';
    end if;
    if v_member.exclusion_reason is not null or v_member.phone is null then
        return jsonb_build_object('ok', false, 'reason_code', 'member_excluded');
    end if;
    begin
        insert into public.bulk_send_prepared (send_id, list_id, position, chatwoot_contact_id,
            chatwoot_conversation_id, chatwoot_source_id, label, how)
        values (p_send_id, v_send.list_id, p_position, (p_prepared ->> 'contact_id')::bigint,
            (p_prepared ->> 'conversation_id')::bigint, p_prepared ->> 'source_id', p_prepared ->> 'label',
            p_prepared ->> 'how')
        on conflict (send_id, position) do nothing;
    exception when check_violation or invalid_text_representation or numeric_value_out_of_range then
        raise exception using errcode = '22023', message = 'bulk_send_prepared_invalid';
    end;
    if not found then
        select chatwoot_conversation_id into v_existing from public.bulk_send_prepared
         where send_id = p_send_id and position = p_position;
        return jsonb_build_object('ok', false, 'reason_code', 'already_prepared',
            'conversation_id', v_existing);
    end if;
    return jsonb_build_object('ok', true, 'position', p_position);
end;
$function$;

-- Lo anotado para unas posiciones (hasta 500), en el orden de la lista. Lo que no esta anotado no vuelve.
create function public.read_bulk_send_prepared_v1(p_send_id uuid, p_positions integer[])
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_send_id is null or p_positions is null or cardinality(p_positions) > 500
       or array_position(p_positions, null) is not null then
        raise exception using errcode = '22023', message = 'bulk_send_read_invalid';
    end if;
    if not exists (select 1 from public.bulk_sends where id = p_send_id) then
        raise exception using errcode = 'P0002', message = 'bulk_send_not_found';
    end if;
    return coalesce((
        select jsonb_agg(jsonb_build_object('position', p.position, 'contact_id', p.chatwoot_contact_id,
                   'conversation_id', p.chatwoot_conversation_id, 'source_id', p.chatwoot_source_id,
                   'label', p.label, 'how', p.how, 'prepared_at', p.prepared_at) order by p.position)
          from public.bulk_send_prepared p
         where p.send_id = p_send_id and p.position = any(p_positions)), '[]'::jsonb);
end;
$function$;

-- Las personas sin exclusion despues de p_after_position (-1 para empezar), en el orden de la lista. No mira
-- bulk_send_recipients ni lo preparado: el cursor es la posicion. Con telefono: solo la lee el bridge.
create function public.read_bulk_send_members_v1(p_send_id uuid, p_after_position integer, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_send public.bulk_sends%rowtype;
begin
    if p_after_position is null or p_after_position < -1
       or p_limit is null or p_limit < 1 or p_limit > 500 then
        raise exception using errcode = '22023', message = 'bulk_send_read_invalid';
    end if;
    select * into v_send from public.bulk_sends where id = p_send_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_send_not_found';
    end if;
    return coalesce((
        select jsonb_agg(jsonb_build_object('position', m.position, 'phone', m.phone, 'first_name', m.first_name)
                         order by m.position)
          from (select m.position, m.phone, m.first_name from public.bulk_list_members m
                 where m.list_id = v_send.list_id and m.exclusion_reason is null and m.phone is not null
                   and m.position > p_after_position
                 order by m.position limit p_limit) m), '[]'::jsonb);
end;
$function$;

-- ===========================================================================
-- 3. El producto del envio: una columna que fija el AE en el borrador.
-- ===========================================================================

-- Los funnel_ref de las compras (portable_hotmart_purchase_correlations), de 1 a 20. Sin repetir: lo
-- controla create_bulk_send_v1 (un check no admite subconsultas).
alter table public.bulk_sends
    add column product_refs text[]
        check (product_refs is null
               or (cardinality(product_refs) between 1 and 20
                   and array_position(product_refs, null) is null
                   and array_to_string(product_refs, ',') ~ '^[a-z0-9_-]{1,100}(,[a-z0-9_-]{1,100})*$'));

-- create_bulk_send_v1 de 20261125000200 con product_refs (opcional: una lista de textos).
create or replace function public.create_bulk_send_v1(p_instance jsonb, p_send jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_tenant text;
    v_ally text;
    v_id uuid;
    v_variable jsonb;
    v_products text[];
begin
    select i.o_tenant_ref, i.o_ally_ref into v_tenant, v_ally from public._flow_store_instance(p_instance) i;
    if jsonb_typeof(p_send) is distinct from 'object'
       or jsonb_typeof(p_send -> 'template_name') is distinct from 'string'
       or jsonb_typeof(p_send -> 'template_language') is distinct from 'string'
       or jsonb_typeof(p_send -> 'scheduled_at') is distinct from 'string'
       or coalesce(jsonb_typeof(p_send -> 'template_variables'), 'array') <> 'array'
       or coalesce(jsonb_typeof(p_send -> 'product_refs'), 'null') not in ('array', 'null') then
        raise exception using errcode = '22023', message = 'bulk_send_invalid';
    end if;
    for v_variable in select value from jsonb_array_elements(coalesce(p_send -> 'template_variables', '[]'::jsonb)) loop
        if not (jsonb_typeof(v_variable) = 'string' and char_length(v_variable #>> '{}') between 1 and 200
                or v_variable = '{"de": "nombre"}'::jsonb) then
            raise exception using errcode = '22023', message = 'bulk_send_variable_invalid';
        end if;
    end loop;
    if jsonb_typeof(p_send -> 'product_refs') = 'array' then
        if exists (select 1 from jsonb_array_elements(p_send -> 'product_refs') e where jsonb_typeof(e) <> 'string') then
            raise exception using errcode = '22023', message = 'bulk_send_invalid';
        end if;
        v_products := array(select e from jsonb_array_elements_text(p_send -> 'product_refs') e);
        if cardinality(v_products) <> (select count(distinct x) from unnest(v_products) x) then
            raise exception using errcode = '22023', message = 'bulk_send_invalid';
        end if;
    end if;
    begin
        insert into public.bulk_sends (tenant_ref, ally_ref, template_name, template_language, template_variables,
            header_image_url, campaign_label, scheduled_at, window_mode, rate_millis, recipient_cap, created_by,
            product_refs)
        values (v_tenant, v_ally, p_send ->> 'template_name', p_send ->> 'template_language',
            coalesce(p_send -> 'template_variables', '[]'::jsonb), p_send ->> 'header_image_url',
            p_send ->> 'campaign_label', (p_send ->> 'scheduled_at')::timestamptz,
            coalesce(p_send ->> 'window_mode', 'centrada'), coalesce((p_send ->> 'rate_millis')::integer, 2500),
            coalesce((p_send ->> 'recipient_cap')::integer, 6000), coalesce(p_send ->> 'created_by', 'automation_expert'),
            v_products)
        returning id into v_id;
    exception when check_violation or invalid_datetime_format or invalid_text_representation
                   or datetime_field_overflow or numeric_value_out_of_range then
        raise exception using errcode = '22023', message = 'bulk_send_invalid';
    end;
    return jsonb_build_object('ok', true, 'send_id', v_id);
end;
$function$;

-- _bulk_send_view de 20261125000200 con product_refs.
create or replace function public._bulk_send_view(p_send_id uuid)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'send_id', s.id, 'state', s.state, 'template_name', s.template_name,
        'template_language', s.template_language, 'template_variables', s.template_variables,
        'header_image_url', s.header_image_url, 'campaign_label', s.campaign_label,
        'product_refs', to_jsonb(s.product_refs),
        'scheduled_at', s.scheduled_at, 'window_mode', s.window_mode, 'rate_millis', s.rate_millis,
        'recipient_cap', s.recipient_cap, 'created_by', s.created_by, 'created_at', s.created_at,
        'confirmed_by', s.confirmed_by, 'confirmed_at', s.confirmed_at, 'confirmed_count', s.confirmed_count,
        'confirmed_health', s.confirmed_health, 'started_at', s.started_at, 'finished_at', s.finished_at,
        'brake_reason', s.brake_reason, 'braked_by', s.braked_by, 'braked_at', s.braked_at,
        'list', case when l.id is null then null else jsonb_build_object(
            'list_id', l.id, 'state', l.state, 'uploaded_by', l.uploaded_by, 'uploaded_at', l.uploaded_at,
            'file_sha256', l.file_sha256, 'source_kind', l.source_kind, 'source_note', l.source_note,
            'notice_declared', l.notice_declared, 'rejected_counts', l.rejected_counts,
            'member_count', l.member_count, 'retention_days', l.retention_days, 'vetted_at', l.vetted_at,
            'purged_at', l.purged_at,
            'eligible_count', (select count(*) from public.bulk_list_members m
                                where m.list_id = l.id and m.exclusion_reason is null)::integer,
            'excluded_by_reason', (select coalesce(jsonb_object_agg(x.exclusion_reason, x.n), '{}'::jsonb)
                from (select m.exclusion_reason, count(*) as n from public.bulk_list_members m
                       where m.list_id = l.id and m.exclusion_reason is not null
                       group by m.exclusion_reason) x),
            'examples', (select coalesce(jsonb_agg(jsonb_build_object(
                    'phone', public._bulk_send_mask(e.phone),
                    'initial', case when e.first_name is null then null else upper(left(e.first_name, 1)) || '.' end)
                    order by e.position), '[]'::jsonb)
                from (select m.position, m.phone, m.first_name from public.bulk_list_members m
                       where m.list_id = l.id and m.exclusion_reason is null and m.phone is not null
                       order by m.position limit 5) e)
        ) end,
        'recipients_by_state', (select coalesce(jsonb_object_agg(r.state, r.n), '{}'::jsonb)
            from (select r.state, count(*) as n from public.bulk_send_recipients r
                   where r.send_id = s.id group by r.state) r))
      from public.bulk_sends s
      left join public.bulk_lists l on l.id = s.list_id
     where s.id = p_send_id
$function$;

-- ===========================================================================
-- 4. Los vetos de la base (encargo 4, #506).
-- ===========================================================================

-- Las compras aprobadas de la instancia sin reembolso ni contracargo: la misma regla que
-- las compras del disparo por audiencia (20261112220000, sin tocarlas), con la huella del mail del comprador ademas del telefono.
-- El mail no sale de aca: solo su huella, igual que la de bulk_list_members.
create function public._bulk_send_purchases(p_tenant_ref text)
returns table (funnel_ref text, phone text, email_fingerprint text)
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select correlation.funnel_ref,
           nullif(regexp_replace(coalesce(event.payload #>> '{data,buyer,checkout_phone}',
                                          event.payload #>> '{data,buyer,phone}', ''), '[^0-9]', '', 'g'), ''),
           case when nullif(btrim(event.payload #>> '{data,buyer,email}'), '') is null then null
                else public._bulk_send_fingerprint('email', lower(btrim(event.payload #>> '{data,buyer,email}'))) end
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
$function$;

-- [{"position": n, "reason": "baja" | "compro"}], una fila por persona y motivo, en el orden de la lista. Solo
-- las personas con telefono (no purgadas), con o sin exclusion: los vetos reemplazan todas las exclusiones.
-- La baja no mira la bandeja ni la aliada: quien pidio que no le escriban no recibe un masivo de nadie.
create function public.read_bulk_list_base_vetoes_v1(p_instance jsonb, p_list_id uuid,
    p_product_refs text[] default null)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_tenant text;
    v_ally text;
    v_send_id uuid;
    v_stored text[];
    v_products text[];
begin
    select i.o_tenant_ref, i.o_ally_ref into v_tenant, v_ally from public._flow_store_instance(p_instance) i;
    if p_product_refs is not null and cardinality(p_product_refs) > 0
       and (cardinality(p_product_refs) > 20 or array_position(p_product_refs, null) is not null
            or array_to_string(p_product_refs, ',') !~ '^[a-z0-9_-]{1,100}(,[a-z0-9_-]{1,100})*$') then
        raise exception using errcode = '22023', message = 'bulk_list_vetoes_invalid';
    end if;
    select l.send_id into v_send_id from public.bulk_lists l
     where l.id = p_list_id and l.tenant_ref = v_tenant and l.ally_ref = v_ally;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_list_not_found';
    end if;
    select s.product_refs into v_stored from public.bulk_sends s where s.id = v_send_id;
    v_products := coalesce(nullif(p_product_refs, '{}'::text[]), v_stored);
    if v_products is null then
        raise exception using errcode = '22023', message = 'bulk_send_products_missing';
    end if;
    if v_stored is not null and not (v_products @> v_stored and v_stored @> v_products) then
        raise exception using errcode = '22023', message = 'bulk_send_products_mismatch';
    end if;
    return coalesce((
        with members as (
            select m.position, m.email_fingerprint, public._whatsapp_phone_variants(m.phone) as variants
              from public.bulk_list_members m
             where m.list_id = p_list_id and m.phone is not null
        ),
        purchases as (
            select p.phone, p.email_fingerprint from public._bulk_send_purchases(v_tenant) p
             where p.funnel_ref = any(v_products)
        ),
        flagged as (
            select m.position, 'baja'::text as reason
              from members m
             where m.variants is not null and (
                   exists (select 1 from public.contact_opt_out_events o
                            where o.channel = 'whatsapp' and o.external_user_id = any(m.variants))
                or exists (select 1 from public.contact_authorizations ca
                            where ca.channel = 'whatsapp' and ca.authorization_status = 'denied'
                              and ca.valid_from <= clock_timestamp()
                              and (ca.valid_until is null or ca.valid_until > clock_timestamp())
                              and ca.contact_id in (
                                  select identity.contact_id from public.channel_identities identity
                                   where identity.channel = 'whatsapp' and identity.external_user_id = any(m.variants)
                                  union
                                  select point.contact_id from public.contact_points point
                                   where point.type = 'phone'
                                     and regexp_replace(point.normalized_value, '[^0-9]', '', 'g') = any(m.variants))))
            union all
            select m.position, 'compro'::text
              from members m
             where exists (select 1 from purchases p
                            where (m.variants is not null and p.phone = any(m.variants))
                               or (m.email_fingerprint is not null and p.email_fingerprint = m.email_fingerprint))
        )
        select jsonb_agg(jsonb_build_object('position', f.position, 'reason', f.reason) order by f.position, f.reason)
          from flagged f), '[]'::jsonb);
end;
$function$;

-- ===========================================================================
-- 5. La purga, ampliada: tambien borra lo preparado del envio de la lista purgada.
--    Misma firma, mismo cuerpo de 20261125000200 mas el borrado y su conteo.
-- ===========================================================================

create or replace function public.purge_bulk_list_phones_v1(p_now timestamptz)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_now timestamptz := coalesce(p_now, clock_timestamp());
    v_list record;
    v_lists integer := 0;
    v_members integer := 0;
    v_prepared integer := 0;
    v_count integer;
begin
    for v_list in
        select l.id, l.send_id from public.bulk_lists l
          join public.bulk_sends s on s.id = l.send_id
         where l.state <> 'purgada'
           and l.uploaded_at + make_interval(days => l.retention_days) <= v_now
           and s.state not in ('preparando', 'enviando')
         order by l.uploaded_at
         for update of l, s
    loop
        update public.bulk_list_members set phone = null, first_name = null
         where list_id = v_list.id and (phone is not null or first_name is not null);
        get diagnostics v_count = row_count;
        v_members := v_members + v_count;
        delete from public.bulk_send_prepared where send_id = v_list.send_id;
        get diagnostics v_count = row_count;
        v_prepared := v_prepared + v_count;
        update public.bulk_lists set state = 'purgada', purged_at = clock_timestamp() where id = v_list.id;
        update public.bulk_sends
           set state = 'frenado', brake_reason = 'lista_purgada', braked_by = 'purga', braked_at = clock_timestamp()
         where id = v_list.send_id and state in ('borrador', 'confirmado');
        v_lists := v_lists + 1;
    end loop;
    return jsonb_build_object('ok', true, 'lists', v_lists, 'members', v_members, 'prepared', v_prepared);
end;
$function$;

-- ===========================================================================
-- 6. Los permisos: la tabla sin acceso por la API; las cuatro funciones nuevas del
--    bridge, solo service_role; la auxiliar, de nadie. Las redefinidas conservan los
--    suyos (create or replace no los toca).
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    execute 'revoke all on table public.bulk_send_prepared from public';
    for v_role in
        select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by 1
    loop
        execute format('revoke all on table public.bulk_send_prepared from %I', v_role);
    end loop;
    foreach v_function in array array[
        'public.record_bulk_send_prepared_v1(uuid,integer,jsonb)',
        'public.read_bulk_send_prepared_v1(uuid,integer[])',
        'public.read_bulk_send_members_v1(uuid,integer,integer)',
        'public.read_bulk_list_base_vetoes_v1(jsonb,uuid,text[])',
        'public._bulk_send_purchases(text)'
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
        grant execute on function public.record_bulk_send_prepared_v1(uuid, integer, jsonb) to service_role;
        grant execute on function public.read_bulk_send_prepared_v1(uuid, integer[]) to service_role;
        grant execute on function public.read_bulk_send_members_v1(uuid, integer, integer) to service_role;
        grant execute on function public.read_bulk_list_base_vetoes_v1(jsonb, uuid, text[]) to service_role;
    end if;
end;
$acl$;

commit;
