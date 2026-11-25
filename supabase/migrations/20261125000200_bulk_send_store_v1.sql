-- bulk-send-store-v1: donde se guardan la lista y cada envio del masivo manejado
-- por el Automation Expert (encargo 3 del mapa «El masivo manejado por el
-- Automation Expert», M1 del hito «Maribel y ATT1 sin GHL»; pieza 4).
--
-- Porta a la base el registro que el kit masivo.py llevaba en archivos sueltos del
-- VPS (el masivo del Black de Maribel del 22/11): la lista ya normalizada, los
-- vetos con su motivo, el envio con su confirmacion por numero escrito y el
-- registro idempotente por persona y envio. Nadie la usa todavia: sin
-- BULK_SEND_ENABLED (encargos siguientes) el bridge no llama a nada de esto.
--
-- 1. Cuatro tablas nuevas, vacias:
--    * bulk_lists: una lista subida por la pagina. Quien la subio (slack:<id>), la
--      huella sha256 del archivo (el archivo crudo no se guarda nunca), el origen
--      declarado y el aviso previo (decision 3), los conteos por motivo de las filas
--      que no sirvieron, los dias que se guardan los telefonos (decision 9: 30 por
--      defecto) y su estado: cargada -> revisada (corrieron los vetos) -> purgada.
--    * bulk_list_members: una fila por persona valida, en el orden del archivo. El
--      telefono en forma de envio, el primer nombre, la huella del telefono y la del
--      mail (para cruzar con compradores sin guardar el mail) y el motivo de
--      exclusion de los vetos. La purga borra telefono y nombre y deja las huellas.
--    * bulk_sends: un envio. Plantilla, idioma, variables, imagen, etiqueta de la
--      campana, hora central, ritmo, ventana, tope (decision 10: 6.000) y estado:
--      borrador -> confirmado -> preparando -> enviando -> terminado, o frenado.
--    * bulk_send_recipients: por persona y envio, reservado -> enviado, fallido,
--      dudoso o vetado, con la conversacion y el mensaje de Chatwoot y el codigo de
--      Meta. La clave (envio, posicion) es la idempotencia: un reservado sin cierre
--      pasa a dudoso y nunca se vuelve a reservar; enviado, dudoso y vetado tampoco;
--      un fallido (error antes de mandar) solo con el reintento pedido a proposito.
-- 2. Funciones del bridge, solo de service_role:
--    * create_bulk_send_v1: el borrador que arma el AE (encargo 6).
--    * apply_bulk_list_vetoes_v1: los vetos (encargo 4) reemplazan las exclusiones
--      y dejan la lista revisada. Sin esto no se puede confirmar.
--    * transition_bulk_send_v1: el motor (encargos 7 y 9) avanza o frena el envio.
--    * reserve_bulk_send_recipient_v1, close_bulk_send_recipient_v1 y
--      expire_bulk_send_reservations_v1: el registro idempotente.
--    * read_bulk_send_v1 y read_bulk_sends_v1: conteos y ejemplos enmascarados,
--      para el AE. Nunca un telefono.
--    * read_bulk_send_batch_v1: la tanda siguiente para el motor (con telefono:
--      solo el bridge la lee).
--    * purge_bulk_list_phones_v1: borra telefono y nombre de las listas vencidas.
-- 3. Funciones de la pagina, solo del rol flow_change_approver, con la sesion de
--    «Entrar con Slack» (20261028000100). Suben y confirman los aprobadores de
--    flujos de la instancia (decision 7); confirmar el propio envio esta permitido.
--    * approver_create_bulk_list_v1: la lista desde filas ya normalizadas por el
--      bridge (bulk_import.py), atada a un envio en borrador.
--    * approver_read_bulk_send_v1: lo mismo que read_bulk_send_v1.
--    * approver_confirm_bulk_send_v1: el numero escrito y «Confirmo». Un numero
--      distinto o sin la marca no confirma; con la salud en amarillo hace falta la
--      segunda marca y en rojo no se confirma (decision 5).
--    * approver_brake_bulk_send_v1: frenar a mano.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente. Correrla dos
-- veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida, P0002 lo que no existe, 28000 la sesion. Los
-- resultados vuelven como {"ok": false, "reason_code"} cuando son de estado.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regrole('flow_change_approver') is null then
        raise exception using errcode = '55000', message = 'bulk_send_store_prerequisite_missing';
    end if;
    if to_regclass('public.bulk_sends') is not null then
        raise exception using errcode = '55000', message = 'bulk_send_store_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. Las tablas.
-- ===========================================================================

create table public.bulk_sends (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null,
    ally_ref text not null,
    template_name text not null check (template_name ~ '^[a-z0-9_]+$' and char_length(template_name) <= 512),
    template_language text not null check (template_language ~ '^[a-z]{2,3}(_[A-Z]{2})?$'),
    -- Las variables del cuerpo, en orden: un texto fijo o {"de": "nombre"} (el primer nombre de la lista).
    template_variables jsonb not null default '[]'::jsonb check (jsonb_typeof(template_variables) = 'array'),
    header_image_url text check (header_image_url is null
        or (header_image_url ~ '^https://[^[:space:]]+$' and char_length(header_image_url) <= 2048)),
    campaign_label text check (campaign_label is null or campaign_label ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    scheduled_at timestamptz not null,
    window_mode text not null default 'centrada' check (window_mode in ('centrada', 'desde_la_hora')),
    -- Personas por segundo, en milesimas: 2500 = 2,5 por segundo (decision 2; techo duro 5).
    rate_millis integer not null default 2500 check (rate_millis between 100 and 5000),
    recipient_cap integer not null default 6000 check (recipient_cap between 1 and 20000),
    list_id uuid,
    state text not null default 'borrador'
        check (state in ('borrador', 'confirmado', 'preparando', 'enviando', 'terminado', 'frenado')),
    created_by text not null check (created_by ~ '^(automation_expert|slack:[UW][A-Z0-9]{8,20})$'),
    created_at timestamptz not null default clock_timestamp(),
    confirmed_by text check (confirmed_by is null or confirmed_by ~ '^slack:[UW][A-Z0-9]{8,20}$'),
    confirmed_at timestamptz,
    confirmed_count integer check (confirmed_count is null or confirmed_count >= 0),
    confirmed_health text check (confirmed_health is null or confirmed_health in ('verde', 'amarillo')),
    started_at timestamptz,
    finished_at timestamptz,
    brake_reason text check (brake_reason is null or brake_reason ~ '^[a-z][a-z0-9_:]{0,63}$'),
    braked_by text check (braked_by is null or braked_by ~ '^(motor|purga|slack:[UW][A-Z0-9]{8,20})$'),
    braked_at timestamptz,
    unique (id, tenant_ref, ally_ref),
    check ((state = 'frenado') = (brake_reason is not null)),
    check ((brake_reason is null) = (braked_at is null) and (brake_reason is null) = (braked_by is null)),
    check ((confirmed_at is null) = (confirmed_by is null)
           and (confirmed_at is null) = (confirmed_count is null)
           and (confirmed_at is null) = (confirmed_health is null)),
    check (state in ('borrador', 'frenado') or confirmed_at is not null),
    check (state = 'borrador' or list_id is not null or state = 'frenado'),
    check ((state = 'terminado') = (finished_at is not null) or state = 'frenado')
);

create table public.bulk_lists (
    id uuid primary key default gen_random_uuid(),
    send_id uuid not null unique,
    tenant_ref text not null,
    ally_ref text not null,
    uploaded_by text not null check (uploaded_by ~ '^slack:[UW][A-Z0-9]{8,20}$'),
    uploaded_at timestamptz not null default clock_timestamp(),
    file_sha256 text not null check (file_sha256 ~ '^[0-9a-f]{64}$'),
    -- De donde sale la lista y que la persona marco que tiene el aviso previo (decision 3).
    source_kind text not null check (source_kind in ('inscritas_ghl', 'compradoras', 'otra')),
    source_note text check (source_note is null or char_length(source_note) between 1 and 280),
    notice_declared boolean not null check (notice_declared),
    -- Las filas que no sirvieron, por motivo (bulk_import.py): {"sin_pais": 3, "duplicado": 2, ...}.
    rejected_counts jsonb not null default '{}'::jsonb check (jsonb_typeof(rejected_counts) = 'object'),
    member_count integer not null check (member_count between 1 and 20000),
    retention_days integer not null default 30 check (retention_days between 1 and 90),
    state text not null default 'cargada' check (state in ('cargada', 'revisada', 'purgada')),
    vetted_at timestamptz,
    purged_at timestamptz,
    foreign key (send_id, tenant_ref, ally_ref) references public.bulk_sends (id, tenant_ref, ally_ref)
        on delete restrict,
    check ((state = 'cargada') = (vetted_at is null) or state = 'purgada'),
    check ((state = 'purgada') = (purged_at is not null))
);

alter table public.bulk_sends
    add constraint bulk_sends_list_fk foreign key (list_id) references public.bulk_lists (id) on delete restrict;

create table public.bulk_list_members (
    list_id uuid not null references public.bulk_lists (id) on delete restrict,
    position integer not null check (position >= 0),
    phone text check (phone is null or phone ~ '^[1-9][0-9]{7,14}$'),
    first_name text check (first_name is null or char_length(first_name) between 1 and 80),
    phone_fingerprint text not null check (phone_fingerprint ~ '^[0-9a-f]{64}$'),
    email_fingerprint text check (email_fingerprint is null or email_fingerprint ~ '^[0-9a-f]{64}$'),
    exclusion_reason text check (exclusion_reason is null or exclusion_reason in (
        'baja', 'baja_ghl', 'compro', 'clienta', 'con_equipo', 'bloqueado', 'escribio_hace_poco')),
    primary key (list_id, position),
    unique (list_id, phone_fingerprint)
);

create table public.bulk_send_recipients (
    send_id uuid not null references public.bulk_sends (id) on delete restrict,
    list_id uuid not null,
    position integer not null,
    state text not null check (state in ('reservado', 'enviado', 'fallido', 'dudoso', 'vetado')),
    attempts integer not null default 1 check (attempts between 1 and 3),
    reserved_at timestamptz not null default clock_timestamp(),
    closed_at timestamptz,
    chatwoot_conversation_id bigint check (chatwoot_conversation_id is null or chatwoot_conversation_id > 0),
    chatwoot_message_id bigint check (chatwoot_message_id is null or chatwoot_message_id > 0),
    meta_error_code text check (meta_error_code is null or meta_error_code ~ '^[0-9]{1,8}$'),
    reason text check (reason is null or reason ~ '^[a-z][a-z0-9_:]{0,63}$'),
    primary key (send_id, position),
    foreign key (list_id, position) references public.bulk_list_members (list_id, position) on delete restrict,
    check ((state = 'reservado') = (closed_at is null)),
    check (state <> 'enviado' or chatwoot_message_id is not null)
);
create index bulk_send_recipients_reserved
    on public.bulk_send_recipients (send_id, reserved_at) where state = 'reservado';
create index bulk_sends_instance on public.bulk_sends (tenant_ref, ally_ref, created_at desc);

alter table public.bulk_sends enable row level security;
alter table public.bulk_lists enable row level security;
alter table public.bulk_list_members enable row level security;
alter table public.bulk_send_recipients enable row level security;

-- ===========================================================================
-- 2. Las auxiliares (de nadie).
-- ===========================================================================

-- La huella de un telefono en forma de envio, o de un mail ya en minusculas.
create function public._bulk_send_fingerprint(p_kind text, p_value text)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select encode(sha256(convert_to('bulk-send:' || p_kind || ':' || p_value, 'UTF8')), 'hex')
$function$;

-- El telefono enmascarado como lo mostraba masivo.py (enmascarar): los ultimos 4 digitos.
create function public._bulk_send_mask(p_phone text)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case when p_phone is null then null else '…' || right(p_phone, 4) end
$function$;

-- Un envio de la instancia, bloqueado; o P0002.
create function public._bulk_send_of(p_tenant_ref text, p_ally_ref text, p_send_id uuid)
returns public.bulk_sends
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_row public.bulk_sends%rowtype;
begin
    select s.* into v_row from public.bulk_sends s
     where s.id = p_send_id and s.tenant_ref = p_tenant_ref and s.ally_ref = p_ally_ref
       for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_send_not_found';
    end if;
    return v_row;
end;
$function$;

-- Lo que la pagina y el AE ven de un envio: conteos y cinco ejemplos enmascarados. Nunca un telefono.
create function public._bulk_send_view(p_send_id uuid)
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
-- 3. Las funciones del bridge (service_role).
-- ===========================================================================

create function public.create_bulk_send_v1(p_instance jsonb, p_send jsonb)
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
begin
    select i.o_tenant_ref, i.o_ally_ref into v_tenant, v_ally from public._flow_store_instance(p_instance) i;
    if jsonb_typeof(p_send) is distinct from 'object'
       or jsonb_typeof(p_send -> 'template_name') is distinct from 'string'
       or jsonb_typeof(p_send -> 'template_language') is distinct from 'string'
       or jsonb_typeof(p_send -> 'scheduled_at') is distinct from 'string'
       or coalesce(jsonb_typeof(p_send -> 'template_variables'), 'array') <> 'array' then
        raise exception using errcode = '22023', message = 'bulk_send_invalid';
    end if;
    for v_variable in select value from jsonb_array_elements(coalesce(p_send -> 'template_variables', '[]'::jsonb)) loop
        if not (jsonb_typeof(v_variable) = 'string' and char_length(v_variable #>> '{}') between 1 and 200
                or v_variable = '{"de": "nombre"}'::jsonb) then
            raise exception using errcode = '22023', message = 'bulk_send_variable_invalid';
        end if;
    end loop;
    begin
        insert into public.bulk_sends (tenant_ref, ally_ref, template_name, template_language, template_variables,
            header_image_url, campaign_label, scheduled_at, window_mode, rate_millis, recipient_cap, created_by)
        values (v_tenant, v_ally, p_send ->> 'template_name', p_send ->> 'template_language',
            coalesce(p_send -> 'template_variables', '[]'::jsonb), p_send ->> 'header_image_url',
            p_send ->> 'campaign_label', (p_send ->> 'scheduled_at')::timestamptz,
            coalesce(p_send ->> 'window_mode', 'centrada'), coalesce((p_send ->> 'rate_millis')::integer, 2500),
            coalesce((p_send ->> 'recipient_cap')::integer, 6000), coalesce(p_send ->> 'created_by', 'automation_expert'))
        returning id into v_id;
    exception when check_violation or invalid_datetime_format or invalid_text_representation
                   or datetime_field_overflow or numeric_value_out_of_range then
        raise exception using errcode = '22023', message = 'bulk_send_invalid';
    end;
    return jsonb_build_object('ok', true, 'send_id', v_id);
end;
$function$;

-- Los vetos de la lista (encargo 4): p_exclusions = [{"position": n, "reason": "baja"}, ...] reemplaza las
-- exclusiones anteriores y deja la lista revisada. Solo mientras el envio esta en borrador.
create function public.apply_bulk_list_vetoes_v1(p_instance jsonb, p_list_id uuid, p_exclusions jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_tenant text;
    v_ally text;
    v_list public.bulk_lists%rowtype;
    v_send public.bulk_sends%rowtype;
    v_marked integer;
    v_given integer;
begin
    select i.o_tenant_ref, i.o_ally_ref into v_tenant, v_ally from public._flow_store_instance(p_instance) i;
    if jsonb_typeof(p_exclusions) is distinct from 'array' then
        raise exception using errcode = '22023', message = 'bulk_list_vetoes_invalid';
    end if;
    select * into v_list from public.bulk_lists
     where id = p_list_id and tenant_ref = v_tenant and ally_ref = v_ally for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_list_not_found';
    end if;
    v_send := public._bulk_send_of(v_tenant, v_ally, v_list.send_id);
    if v_send.state <> 'borrador' or v_list.state = 'purgada' then
        return jsonb_build_object('ok', false, 'reason_code', 'not_a_draft', 'state', v_send.state);
    end if;
    if exists (select 1 from jsonb_array_elements(p_exclusions) e
                where jsonb_typeof(e -> 'position') is distinct from 'number'
                   or (e ->> 'reason') is null
                   or (e ->> 'reason') not in ('baja', 'baja_ghl', 'compro', 'clienta', 'con_equipo', 'bloqueado',
                                               'escribio_hace_poco'))
       or (select count(*) from jsonb_array_elements(p_exclusions))
          <> (select count(distinct e ->> 'position') from jsonb_array_elements(p_exclusions) e) then
        raise exception using errcode = '22023', message = 'bulk_list_vetoes_invalid';
    end if;
    v_given := jsonb_array_length(p_exclusions);
    update public.bulk_list_members set exclusion_reason = null where list_id = p_list_id;
    update public.bulk_list_members m set exclusion_reason = e.reason
      from (select (x ->> 'position')::integer as position, x ->> 'reason' as reason
              from jsonb_array_elements(p_exclusions) x) e
     where m.list_id = p_list_id and m.position = e.position;
    get diagnostics v_marked = row_count;
    if v_marked <> v_given then
        raise exception using errcode = '22023', message = 'bulk_list_vetoes_unknown_position';
    end if;
    update public.bulk_lists set state = 'revisada', vetted_at = clock_timestamp() where id = p_list_id;
    return jsonb_build_object('ok', true, 'excluded', v_marked,
        'eligible', v_list.member_count - v_marked);
end;
$function$;

-- El motor avanza el envio: confirmado -> preparando -> enviando -> terminado; frenado desde cualquiera que
-- no termino. Un frenado o terminado no se mueve mas.
create function public.transition_bulk_send_v1(p_instance jsonb, p_send_id uuid, p_to text, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_tenant text;
    v_ally text;
    v_row public.bulk_sends%rowtype;
begin
    select i.o_tenant_ref, i.o_ally_ref into v_tenant, v_ally from public._flow_store_instance(p_instance) i;
    if p_to is null or p_to not in ('preparando', 'enviando', 'terminado', 'frenado')
       or (p_to = 'frenado') is distinct from (p_reason is not null)
       or (p_reason is not null and p_reason !~ '^[a-z][a-z0-9_:]{0,63}$') then
        raise exception using errcode = '22023', message = 'bulk_send_transition_invalid';
    end if;
    v_row := public._bulk_send_of(v_tenant, v_ally, p_send_id);
    if not (v_row.state, p_to) in (('confirmado', 'preparando'), ('confirmado', 'enviando'),
                                   ('preparando', 'enviando'), ('enviando', 'terminado'),
                                   ('borrador', 'frenado'), ('confirmado', 'frenado'),
                                   ('preparando', 'frenado'), ('enviando', 'frenado')) then
        return jsonb_build_object('ok', false, 'reason_code', 'transition_not_allowed', 'state', v_row.state);
    end if;
    if p_to = 'terminado' and exists (select 1 from public.bulk_send_recipients
                                        where send_id = p_send_id and state = 'reservado') then
        return jsonb_build_object('ok', false, 'reason_code', 'reservations_open');
    end if;
    update public.bulk_sends
       set state = p_to,
           started_at = case when p_to = 'enviando' then clock_timestamp() else started_at end,
           finished_at = case when p_to = 'terminado' then clock_timestamp() else finished_at end,
           brake_reason = case when p_to = 'frenado' then p_reason end,
           braked_by = case when p_to = 'frenado' then 'motor' end,
           braked_at = case when p_to = 'frenado' then clock_timestamp() end
     where id = p_send_id;
    return jsonb_build_object('ok', true, 'state', p_to);
end;
$function$;

-- Reservar a una persona antes de mandarle (idempotente por envio y posicion). Solo con el envio en curso y la
-- persona sin exclusion. Lo ya registrado no se vuelve a reservar: enviado, dudoso, vetado y reservado nunca;
-- fallido solo con p_retry_failed (error de la API antes de mandar), hasta 3 intentos.
create function public.reserve_bulk_send_recipient_v1(p_send_id uuid, p_position integer, p_retry_failed boolean)
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
    v_existing public.bulk_send_recipients%rowtype;
begin
    if p_send_id is null or p_position is null or p_position < 0 then
        raise exception using errcode = '22023', message = 'bulk_send_reserve_invalid';
    end if;
    select * into v_send from public.bulk_sends where id = p_send_id for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_send_not_found';
    end if;
    if v_send.state <> 'enviando' then
        return jsonb_build_object('ok', false, 'reason_code', 'not_sending', 'state', v_send.state);
    end if;
    select * into v_member from public.bulk_list_members where list_id = v_send.list_id and position = p_position;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_list_member_not_found';
    end if;
    if v_member.exclusion_reason is not null or v_member.phone is null then
        return jsonb_build_object('ok', false, 'reason_code', 'member_excluded');
    end if;
    select * into v_existing from public.bulk_send_recipients
     where send_id = p_send_id and position = p_position for update;
    if found then
        if v_existing.state = 'fallido' and p_retry_failed is true and v_existing.attempts < 3 then
            update public.bulk_send_recipients
               set state = 'reservado', attempts = attempts + 1, reserved_at = clock_timestamp(),
                   closed_at = null, reason = null, meta_error_code = null
             where send_id = p_send_id and position = p_position;
            return jsonb_build_object('ok', true, 'phone', v_member.phone, 'first_name', v_member.first_name,
                'attempt', v_existing.attempts + 1);
        end if;
        return jsonb_build_object('ok', false, 'reason_code', 'already_' || v_existing.state);
    end if;
    insert into public.bulk_send_recipients (send_id, list_id, position, state)
    values (p_send_id, v_send.list_id, p_position, 'reservado')
    on conflict (send_id, position) do nothing;
    if not found then
        return jsonb_build_object('ok', false, 'reason_code', 'already_reservado');
    end if;
    return jsonb_build_object('ok', true, 'phone', v_member.phone, 'first_name', v_member.first_name, 'attempt', 1);
end;
$function$;

-- Cerrar una reserva con lo que paso. Solo desde reservado: un cierre repetido no cambia nada.
create function public.close_bulk_send_recipient_v1(p_send_id uuid, p_position integer, p_outcome jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_state text;
begin
    if p_send_id is null or p_position is null or jsonb_typeof(p_outcome) is distinct from 'object'
       or (p_outcome ->> 'state') is null or (p_outcome ->> 'state') not in ('enviado', 'fallido', 'dudoso', 'vetado') then
        raise exception using errcode = '22023', message = 'bulk_send_close_invalid';
    end if;
    select state into v_state from public.bulk_send_recipients
     where send_id = p_send_id and position = p_position for update;
    if not found then
        return jsonb_build_object('ok', false, 'reason_code', 'not_reserved');
    end if;
    if v_state <> 'reservado' then
        return jsonb_build_object('ok', false, 'reason_code', 'already_' || v_state);
    end if;
    begin
        update public.bulk_send_recipients
           set state = p_outcome ->> 'state', closed_at = clock_timestamp(),
               chatwoot_conversation_id = (p_outcome ->> 'chatwoot_conversation_id')::bigint,
               chatwoot_message_id = (p_outcome ->> 'chatwoot_message_id')::bigint,
               meta_error_code = p_outcome ->> 'meta_error_code', reason = p_outcome ->> 'reason'
         where send_id = p_send_id and position = p_position;
    exception when check_violation or invalid_text_representation or numeric_value_out_of_range then
        raise exception using errcode = '22023', message = 'bulk_send_close_invalid';
    end;
    return jsonb_build_object('ok', true, 'state', p_outcome ->> 'state');
end;
$function$;

-- Las reservas sin cierre de mas de p_minutes (el bridge se cayo entre reservar y cerrar) pasan a dudoso: no se
-- sabe si salio, y no se reintentan nunca.
create function public.expire_bulk_send_reservations_v1(p_send_id uuid, p_minutes integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_count integer;
begin
    if p_send_id is null or p_minutes is null or p_minutes < 1 or p_minutes > 1440 then
        raise exception using errcode = '22023', message = 'bulk_send_expire_invalid';
    end if;
    update public.bulk_send_recipients
       set state = 'dudoso', closed_at = clock_timestamp(), reason = 'reservado_sin_cierre'
     where send_id = p_send_id and state = 'reservado'
       and reserved_at <= clock_timestamp() - make_interval(mins => p_minutes);
    get diagnostics v_count = row_count;
    return jsonb_build_object('ok', true, 'dudosos', v_count);
end;
$function$;

-- La tanda siguiente del motor: las personas sin exclusion y sin registro en este envio, en el orden de la
-- lista. Con telefono: solo la lee el bridge.
create function public.read_bulk_send_batch_v1(p_send_id uuid, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_send public.bulk_sends%rowtype;
begin
    if p_limit is null or p_limit < 1 or p_limit > 500 then
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
                   and not exists (select 1 from public.bulk_send_recipients r
                                    where r.send_id = p_send_id and r.position = m.position)
                 order by m.position limit p_limit) m), '[]'::jsonb);
end;
$function$;

create function public.read_bulk_send_v1(p_instance jsonb, p_send_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_tenant text;
    v_ally text;
begin
    select i.o_tenant_ref, i.o_ally_ref into v_tenant, v_ally from public._flow_store_instance(p_instance) i;
    if not exists (select 1 from public.bulk_sends
                    where id = p_send_id and tenant_ref = v_tenant and ally_ref = v_ally) then
        raise exception using errcode = 'P0002', message = 'bulk_send_not_found';
    end if;
    return public._bulk_send_view(p_send_id);
end;
$function$;

create function public.read_bulk_sends_v1(p_instance jsonb, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_tenant text;
    v_ally text;
begin
    if p_limit is null or p_limit < 1 or p_limit > 200 then
        raise exception using errcode = '22023', message = 'bulk_send_read_invalid';
    end if;
    select i.o_tenant_ref, i.o_ally_ref into v_tenant, v_ally from public._flow_store_instance(p_instance) i;
    return coalesce((
        select jsonb_agg(public._bulk_send_view(s.id) order by s.created_at desc)
          from (select id, created_at from public.bulk_sends
                 where tenant_ref = v_tenant and ally_ref = v_ally
                 order by created_at desc limit p_limit) s), '[]'::jsonb);
end;
$function$;

-- La purga (decision 9): a los retention_days de subida se borran telefono y nombre; quedan las huellas y los
-- conteos. Una lista con el envio en curso (preparando o enviando) espera; un envio en borrador o confirmado
-- de una lista purgada queda frenado (ya no tiene a quien mandarle).
create function public.purge_bulk_list_phones_v1(p_now timestamptz)
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
        update public.bulk_lists set state = 'purgada', purged_at = clock_timestamp() where id = v_list.id;
        update public.bulk_sends
           set state = 'frenado', brake_reason = 'lista_purgada', braked_by = 'purga', braked_at = clock_timestamp()
         where id = v_list.send_id and state in ('borrador', 'confirmado');
        v_lists := v_lists + 1;
    end loop;
    return jsonb_build_object('ok', true, 'lists', v_lists, 'members', v_members);
end;
$function$;

-- ===========================================================================
-- 4. Las funciones de la pagina (rol flow_change_approver, con la sesion).
-- ===========================================================================

-- La lista desde filas ya normalizadas por el bridge (bulk_import.read_bulk_list):
-- p_list = {"file_sha256", "source_kind", "source_note", "notice_declared", "rejected_counts",
--           "retention_days", "rows": [{"phone", "first_name", "email"}, ...]}.
-- El mail no se guarda: solo su huella. Un telefono repetido o fuera de forma rechaza la lista entera.
create function public.approver_create_bulk_list_v1(
    p_instance jsonb, p_session_hash text, p_send_id uuid, p_list jsonb)
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
    v_send public.bulk_sends%rowtype;
    v_list_id uuid;
    v_rows integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    v_send := public._bulk_send_of(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_send_id);
    if v_send.state <> 'borrador' then
        return jsonb_build_object('ok', false, 'reason_code', 'not_a_draft', 'state', v_send.state);
    end if;
    if v_send.list_id is not null then
        return jsonb_build_object('ok', false, 'reason_code', 'list_already_attached');
    end if;
    if jsonb_typeof(p_list) is distinct from 'object' or jsonb_typeof(p_list -> 'rows') is distinct from 'array'
       or jsonb_typeof(p_list -> 'notice_declared') is distinct from 'boolean'
       or coalesce(jsonb_typeof(p_list -> 'rejected_counts'), 'object') <> 'object'
       or exists (select 1 from jsonb_each(coalesce(p_list -> 'rejected_counts', '{}'::jsonb)) c
                   where c.key !~ '^[a-z][a-z0-9_]{0,31}$' or jsonb_typeof(c.value) <> 'number'
                      or (c.value #>> '{}') !~ '^[0-9]{1,6}$')
       or exists (select 1 from jsonb_array_elements(p_list -> 'rows') r
                   where jsonb_typeof(r) <> 'object'
                      or jsonb_typeof(r -> 'phone') is distinct from 'string'
                      or coalesce(jsonb_typeof(r -> 'first_name'), 'null') not in ('string', 'null')
                      or coalesce(jsonb_typeof(r -> 'email'), 'null') not in ('string', 'null')) then
        raise exception using errcode = '22023', message = 'bulk_list_invalid';
    end if;
    if (p_list ->> 'notice_declared')::boolean is not true then
        return jsonb_build_object('ok', false, 'reason_code', 'notice_not_declared');
    end if;
    v_rows := jsonb_array_length(p_list -> 'rows');
    if v_rows = 0 then
        return jsonb_build_object('ok', false, 'reason_code', 'empty_list');
    end if;
    if v_rows > v_send.recipient_cap then
        return jsonb_build_object('ok', false, 'reason_code', 'over_cap', 'rows', v_rows,
            'recipient_cap', v_send.recipient_cap);
    end if;
    begin
        insert into public.bulk_lists (send_id, tenant_ref, ally_ref, uploaded_by, file_sha256, source_kind,
            source_note, notice_declared, rejected_counts, member_count, retention_days)
        values (p_send_id, v_instance.o_tenant_ref, v_instance.o_ally_ref, 'slack:' || v_actor,
            p_list ->> 'file_sha256', p_list ->> 'source_kind', nullif(btrim(p_list ->> 'source_note'), ''),
            true, coalesce(p_list -> 'rejected_counts', '{}'::jsonb), v_rows,
            coalesce((p_list ->> 'retention_days')::integer, 30))
        returning id into v_list_id;
        insert into public.bulk_list_members (list_id, position, phone, first_name, phone_fingerprint,
            email_fingerprint)
        select v_list_id, (r.ordinality - 1)::integer, r.value ->> 'phone',
               nullif(btrim(r.value ->> 'first_name'), ''),
               public._bulk_send_fingerprint('phone', r.value ->> 'phone'),
               case when nullif(btrim(r.value ->> 'email'), '') is null then null
                    else public._bulk_send_fingerprint('email', lower(btrim(r.value ->> 'email'))) end
          from jsonb_array_elements(p_list -> 'rows') with ordinality r;
    exception
        when unique_violation then
            raise exception using errcode = '22023', message = 'bulk_list_duplicate_phone';
        when check_violation or invalid_text_representation or numeric_value_out_of_range then
            raise exception using errcode = '22023', message = 'bulk_list_invalid';
    end;
    update public.bulk_sends set list_id = v_list_id where id = p_send_id;
    return jsonb_build_object('ok', true, 'list_id', v_list_id, 'member_count', v_rows);
end;
$function$;

create function public.approver_read_bulk_send_v1(p_instance jsonb, p_session_hash text, p_send_id uuid)
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
    if not exists (select 1 from public.bulk_sends where id = p_send_id
                    and tenant_ref = v_instance.o_tenant_ref and ally_ref = v_instance.o_ally_ref) then
        raise exception using errcode = 'P0002', message = 'bulk_send_not_found';
    end if;
    return public._bulk_send_view(p_send_id);
end;
$function$;

-- El numero escrito y «Confirmo» (p_confirmed). El numero es el de las personas que van (sin las excluidas por
-- los vetos). p_health es la salud que la pagina le mostro a la persona: en amarillo hace falta p_yellow_ack, en
-- rojo (o cualquier otra cosa) no confirma.
create function public.approver_confirm_bulk_send_v1(
    p_instance jsonb, p_session_hash text, p_send_id uuid, p_expected_count integer, p_confirmed boolean,
    p_health text, p_yellow_ack boolean)
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
    v_send public.bulk_sends%rowtype;
    v_list public.bulk_lists%rowtype;
    v_eligible integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    v_send := public._bulk_send_of(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_send_id);
    if v_send.state <> 'borrador' then
        return jsonb_build_object('ok', false, 'reason_code', 'not_waiting_for_confirmation', 'state', v_send.state);
    end if;
    if v_send.list_id is null then
        return jsonb_build_object('ok', false, 'reason_code', 'list_missing');
    end if;
    select * into v_list from public.bulk_lists where id = v_send.list_id for update;
    if v_list.state <> 'revisada' then
        return jsonb_build_object('ok', false, 'reason_code', 'list_not_vetted', 'list_state', v_list.state);
    end if;
    if p_confirmed is distinct from true then
        return jsonb_build_object('ok', false, 'reason_code', 'confirmation_missing');
    end if;
    if p_health is null or p_health not in ('verde', 'amarillo') then
        return jsonb_build_object('ok', false, 'reason_code', 'health_blocks');
    end if;
    if p_health = 'amarillo' and p_yellow_ack is distinct from true then
        return jsonb_build_object('ok', false, 'reason_code', 'yellow_ack_missing');
    end if;
    select count(*) into v_eligible from public.bulk_list_members
     where list_id = v_send.list_id and exclusion_reason is null and phone is not null;
    if p_expected_count is distinct from v_eligible then
        return jsonb_build_object('ok', false, 'reason_code', 'count_mismatch', 'eligible_count', v_eligible);
    end if;
    if v_eligible = 0 then
        return jsonb_build_object('ok', false, 'reason_code', 'nobody_to_send');
    end if;
    if v_eligible > v_send.recipient_cap then
        return jsonb_build_object('ok', false, 'reason_code', 'over_cap', 'eligible_count', v_eligible,
            'recipient_cap', v_send.recipient_cap);
    end if;
    update public.bulk_sends
       set state = 'confirmado', confirmed_by = 'slack:' || v_actor, confirmed_at = clock_timestamp(),
           confirmed_count = v_eligible, confirmed_health = p_health
     where id = p_send_id;
    return jsonb_build_object('ok', true, 'confirmed_count', v_eligible);
end;
$function$;

create function public.approver_brake_bulk_send_v1(p_instance jsonb, p_session_hash text, p_send_id uuid)
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
    v_send public.bulk_sends%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    v_send := public._bulk_send_of(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_send_id);
    if v_send.state not in ('borrador', 'confirmado', 'preparando', 'enviando') then
        return jsonb_build_object('ok', false, 'reason_code', 'not_brakeable', 'state', v_send.state);
    end if;
    update public.bulk_sends
       set state = 'frenado', brake_reason = 'a_mano', braked_by = 'slack:' || v_actor, braked_at = clock_timestamp()
     where id = p_send_id;
    return jsonb_build_object('ok', true);
end;
$function$;

-- ===========================================================================
-- 5. Los permisos: tablas sin acceso por la API; las del bridge, solo
--    service_role; las de la pagina, solo flow_change_approver; las internas,
--    para nadie.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_table text;
    v_role text;
begin
    foreach v_table in array array['public.bulk_sends', 'public.bulk_lists', 'public.bulk_list_members',
                                   'public.bulk_send_recipients'] loop
        execute format('revoke all on table %s from public', v_table);
        for v_role in
            select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by 1
        loop
            execute format('revoke all on table %s from %I', v_table, v_role);
        end loop;
    end loop;
    foreach v_function in array array[
        'public._bulk_send_fingerprint(text,text)',
        'public._bulk_send_mask(text)',
        'public._bulk_send_of(text,text,uuid)',
        'public._bulk_send_view(uuid)',
        'public.create_bulk_send_v1(jsonb,jsonb)',
        'public.apply_bulk_list_vetoes_v1(jsonb,uuid,jsonb)',
        'public.transition_bulk_send_v1(jsonb,uuid,text,text)',
        'public.reserve_bulk_send_recipient_v1(uuid,integer,boolean)',
        'public.close_bulk_send_recipient_v1(uuid,integer,jsonb)',
        'public.expire_bulk_send_reservations_v1(uuid,integer)',
        'public.read_bulk_send_batch_v1(uuid,integer)',
        'public.read_bulk_send_v1(jsonb,uuid)',
        'public.read_bulk_sends_v1(jsonb,integer)',
        'public.purge_bulk_list_phones_v1(timestamptz)',
        'public.approver_create_bulk_list_v1(jsonb,text,uuid,jsonb)',
        'public.approver_read_bulk_send_v1(jsonb,text,uuid)',
        'public.approver_confirm_bulk_send_v1(jsonb,text,uuid,integer,boolean,text,boolean)',
        'public.approver_brake_bulk_send_v1(jsonb,text,uuid)'
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
        grant execute on function public.create_bulk_send_v1(jsonb, jsonb) to service_role;
        grant execute on function public.apply_bulk_list_vetoes_v1(jsonb, uuid, jsonb) to service_role;
        grant execute on function public.transition_bulk_send_v1(jsonb, uuid, text, text) to service_role;
        grant execute on function public.reserve_bulk_send_recipient_v1(uuid, integer, boolean) to service_role;
        grant execute on function public.close_bulk_send_recipient_v1(uuid, integer, jsonb) to service_role;
        grant execute on function public.expire_bulk_send_reservations_v1(uuid, integer) to service_role;
        grant execute on function public.read_bulk_send_batch_v1(uuid, integer) to service_role;
        grant execute on function public.read_bulk_send_v1(jsonb, uuid) to service_role;
        grant execute on function public.read_bulk_sends_v1(jsonb, integer) to service_role;
        grant execute on function public.purge_bulk_list_phones_v1(timestamptz) to service_role;
    end if;
    grant execute on function public.approver_create_bulk_list_v1(jsonb, text, uuid, jsonb) to flow_change_approver;
    grant execute on function public.approver_read_bulk_send_v1(jsonb, text, uuid) to flow_change_approver;
    grant execute on function public.approver_confirm_bulk_send_v1(jsonb, text, uuid, integer, boolean, text, boolean)
        to flow_change_approver;
    grant execute on function public.approver_brake_bulk_send_v1(jsonb, text, uuid) to flow_change_approver;
end;
$acl$;

commit;
