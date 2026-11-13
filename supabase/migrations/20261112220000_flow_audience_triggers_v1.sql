-- flow-audience-triggers-v1: el disparo por audiencia (fecha, cada, manual) y su
-- lista congelada (E-484, H6 del mapa «el AE maneja todos los flujos").
-- Contrato: docs/contracts/flow-audience-trigger-v1.md (0.2.0).
--
-- Decision de Dan del 12/11: construir ya los disparadores por fecha, cada cierto
-- tiempo y manual. El AE arma el envio, una persona lo confirma en la pagina viendo
-- cuantos van a salir, y los envios se frenan solos si baja la salud del numero.
-- Un solo mecanismo: cada ocurrencia materializa la audiencia en una lista
-- congelada, idempotente por (flujo, version, ocurrencia), que se suelta al ritmo
-- por hora y pasa por la puerta comun del motor nativo.
--
-- 1. Dos tablas nuevas, vacias: flow_audience_occurrences (una fila por
--    ocurrencia; la unica (flow_id, flow_version, occurrence_key) es lo que hace
--    que un reinicio del bridge no la vuelva a armar) y flow_audience_members (una
--    fila por persona, con su estado: pendiente, liberada u omitida y el motivo;
--    y, una vez liberada, si el motor le abrio el recorrido).
-- 2. Funciones del bridge, solo de service_role:
--    * read_flow_audience_flows_v1: los flujos de audiencia que corren (prendidos
--      o en cohorte) con su definicion activa.
--    * read_flow_audience_candidates_v1: la audiencia posible de un flujo, sin
--      nombre ni mail: prospectas (el formulario con permiso de WhatsApp en la
--      ventana, del producto del flujo) o clientas (una compra aprobada sin
--      reembolso). Con su contacto, su baja y los productos que compro.
--    * materialize_flow_audience_occurrence_v1: arma la lista. Vuelve a mirar,
--      como segunda capa, la baja, la cohorte, el permiso (formulario en la
--      ventana o compra) y la repeticion (topes.repeticion: la misma persona no
--      recibe otra ocurrencia del mismo flujo antes de otra_despues_de). Quien no
--      pasa queda omitida con su motivo. fecha y cada nacen confirmadas (la
--      aprobacion fue al publicar el flujo); manual nace materializada y espera a
--      una persona (D7).
--    * brake_flow_audience_occurrence_v1: el freno por la salud de Meta (D8). Una
--      ocurrencia frenada no se suelta nunca mas.
--    * release_flow_audience_members_v1: suelta a lo sumo ritmo por hora menos lo
--      que ya solto en la ultima hora, en el orden de la lista; y vuelve a dar las
--      liberadas que el motor no asento en 10 minutos (el bridge se cayo entre
--      soltar y abrir), sin contarlas otra vez en el ritmo.
--    * settle_flow_audience_member_v1: lo que contesto el motor al abrir el
--      recorrido (arranco, no_arranca, sin_flujo). La ocurrencia termina cuando no
--      queda nadie pendiente ni sin asentar.
--    * read_flow_audience_occurrences_v1: las ocurrencias de la instancia, con sus
--      conteos (para /ready). Sin telefonos.
-- 3. Funciones de la pagina, solo del rol flow_change_approver, con la sesion de
--    «Entrar con Slack» (la misma de 20261028000100):
--    * approver_read_flow_audience_v1: los flujos manuales que corren y las
--      ocurrencias, con cuantas van a salir y cuantas quedaron afuera por motivo.
--    * approver_confirm_flow_audience_v1: la confirmacion de una persona, con el
--      conteo exacto que vio y el segundo paso. Un conteo distinto no confirma.
--    * approver_brake_flow_audience_v1: frenar a mano una ocurrencia.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente. Sin
-- FLOW_AUDIENCE_TRIGGERS_ENABLED el bridge no llama a nada de esto, y hasta que el
-- motor nativo admita un recorrido de audiencia el validador no deja publicar un
-- flujo asi como corriendo (motor_nativo:disparador). Correrla dos veces aborta con
-- 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida, P0002 lo que no existe, 28000 la sesion. Los
-- resultados vuelven como {"ok": false, "reason_code"} cuando son de estado.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.flow_controls') is null
       or to_regclass('public.flow_definitions') is null
       or to_regclass('public.flow_cohort_members') is null
       or to_regclass('public.contact_authorizations') is null
       or to_regclass('public.purchase_intents') is null
       or to_regclass('public.portable_hotmart_purchase_correlations') is null
       or to_regprocedure('public._whatsapp_phone_variants(text)') is null
       or to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regrole('flow_change_approver') is null then
        raise exception using errcode = '55000', message = 'flow_audience_triggers_prerequisite_missing';
    end if;
    if to_regclass('public.flow_audience_occurrences') is not null then
        raise exception using errcode = '55000', message = 'flow_audience_triggers_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. Las tablas.
-- ===========================================================================

create table public.flow_audience_occurrences (
    id uuid primary key default gen_random_uuid(),
    flow_id uuid not null references public.flow_controls (flow_id) on delete restrict,
    flow_version integer not null check (flow_version > 0),
    occurrence_key text not null
        check (occurrence_key ~ '^(fecha:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z|cada:[0-9]{4}-[0-9]{2}-[0-9]{2}|manual:[0-9a-f-]{36})$'),
    form text not null check (form in ('fecha', 'cada', 'manual')),
    scheduled_at timestamptz not null,
    audience_fingerprint text not null check (audience_fingerprint ~ '^[0-9a-f]{64}$'),
    per_hour integer not null check (per_hour between 1 and 1000),
    per_occurrence integer not null check (per_occurrence between 1 and 5000),
    state text not null
        check (state in ('materializada', 'confirmada', 'enviando', 'terminada', 'frenada')),
    member_count integer not null check (member_count >= 0),
    omitted_count integer not null check (omitted_count >= 0),
    materialized_by text not null check (materialized_by ~ '^(barredor|slack:[UW][A-Z0-9]{8,20})$'),
    materialized_at timestamptz not null default clock_timestamp(),
    confirmed_by text check (confirmed_by is null or confirmed_by ~ '^(publicacion|slack:[UW][A-Z0-9]{8,20})$'),
    confirmed_at timestamptz,
    brake_reason text check (brake_reason is null or brake_reason ~ '^[a-z][a-z0-9_:]{0,63}$'),
    braked_at timestamptz,
    finished_at timestamptz,
    unique (flow_id, flow_version, occurrence_key),
    check ((form = 'manual') = (occurrence_key like 'manual:%')),
    check (form = 'manual' or occurrence_key like form || ':%'),
    check ((state = 'frenada') = (brake_reason is not null)),
    check (state = 'materializada' or state = 'frenada' or confirmed_at is not null)
);

create table public.flow_audience_members (
    occurrence_id uuid not null references public.flow_audience_occurrences (id) on delete restrict,
    position integer not null check (position >= 0),
    contact_id uuid not null,
    normalized_phone text not null check (normalized_phone ~ '^[1-9][0-9]{7,14}$'),
    state text not null check (state in ('pendiente', 'liberada', 'omitida')),
    omit_reason text check (omit_reason is null or omit_reason in ('baja', 'sin_permiso', 'fuera_de_cohorte', 'repetida')),
    released_at timestamptz,
    opened_at timestamptz,
    open_outcome text check (open_outcome is null or open_outcome in ('arranco', 'no_arranca', 'sin_flujo')),
    primary key (occurrence_id, position),
    unique (occurrence_id, contact_id),
    check ((state = 'omitida') = (omit_reason is not null)),
    check ((state = 'liberada') = (released_at is not null)),
    check ((opened_at is null) = (open_outcome is null)),
    check (opened_at is null or state = 'liberada')
);
create index flow_audience_members_released
    on public.flow_audience_members (occurrence_id, released_at) where state = 'liberada';
create index flow_audience_members_contact
    on public.flow_audience_members (contact_id, released_at) where state = 'liberada';

alter table public.flow_audience_occurrences enable row level security;
alter table public.flow_audience_members enable row level security;

-- ===========================================================================
-- 2. Las funciones del bridge.
-- ===========================================================================

-- Las que miran la definicion, en un solo lugar: si es un disparo por audiencia.
create function public._flow_audience_form(p_definition jsonb)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case when jsonb_typeof(p_definition -> 'dispara') = 'object' then
        case when (select count(*) from jsonb_object_keys(p_definition -> 'dispara')) = 1
             then (select k from jsonb_object_keys(p_definition -> 'dispara') k where k in ('fecha', 'cada', 'manual'))
        end
    end
$function$;

-- La forma de la definicion activa del flujo de la instancia, o una excepcion.
create function public._flow_audience_active(p_instance jsonb, p_flow_id uuid, p_version integer,
    out o_state text, out o_tenant_ref text, out o_product_ref text, out o_definition jsonb)
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_ally text;
begin
    select i.o_tenant_ref, i.o_ally_ref into o_tenant_ref, v_ally from public._flow_store_instance(p_instance) i;
    select c.state, c.product_ref, d.definition into o_state, o_product_ref, o_definition
      from public.flow_controls c
      join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
     where c.flow_id = p_flow_id and c.tenant_ref = o_tenant_ref and c.ally_ref = v_ally
       and c.active_version = p_version;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_audience_flow_not_found';
    end if;
end;
$function$;

-- Las compras aprobadas vigentes de un tenant (sin reembolso ni contracargo
-- posterior de la misma transaccion), con el telefono de quien compro en digitos.
-- La misma lectura que get_customer_status_v1 (20261103000700), por telefono.
create function public._flow_audience_purchases(p_tenant_ref text)
returns table (funnel_ref text, phone text)
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select correlation.funnel_ref,
           nullif(regexp_replace(coalesce(event.payload #>> '{data,buyer,checkout_phone}',
                                          event.payload #>> '{data,buyer,phone}', ''), '[^0-9]', '', 'g'), '')
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

create function public.read_flow_audience_flows_v1(p_instance jsonb)
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
    return coalesce((
        select jsonb_agg(jsonb_build_object(
                   'flow_id', c.flow_id, 'flow_key', c.flow_key, 'product_ref', c.product_ref, 'state', c.state,
                   'version', c.active_version, 'definition', d.definition)
                 order by c.product_ref, c.flow_key)
          from public.flow_controls c
          join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
         where c.tenant_ref = v_tenant and c.ally_ref = v_ally and c.state in ('prendido', 'cohorte')
           and public._flow_audience_form(d.definition) is not null
    ), '[]'::jsonb);
end;
$function$;

create function public.read_flow_audience_candidates_v1(p_instance jsonb, p_flow_id uuid, p_version integer,
    p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_state text;
    v_tenant text;
    v_product text;
    v_definition jsonb;
    v_status text;
    v_window integer;
    v_result jsonb;
begin
    if p_limit is null or p_limit < 1 or p_limit > 20000 then
        raise exception using errcode = '22023', message = 'flow_audience_read_invalid';
    end if;
    select a.o_state, a.o_tenant_ref, a.o_product_ref, a.o_definition
      into v_state, v_tenant, v_product, v_definition
      from public._flow_audience_active(p_instance, p_flow_id, p_version) a;
    if public._flow_audience_form(v_definition) is null then
        raise exception using errcode = '22023', message = 'flow_audience_not_an_audience';
    end if;
    v_status := v_definition #>> '{para,es}';
    if v_status not in ('prospecta', 'clienta') then
        raise exception using errcode = '22023', message = 'flow_audience_status_invalid';
    end if;
    -- para.formulario_en: la misma regla que el validador (flow_audience.form_window_days) y la admision
    -- (20261112210600): de 1d a 365d, 30d sin la clave; otra cosa no es una ventana.
    v_window := case when coalesce(v_definition #>> '{para,formulario_en}', '30d') ~ '^[1-9][0-9]{0,2}d$'
                     then left(coalesce(v_definition #>> '{para,formulario_en}', '30d'), -1)::integer end;
    if v_window is null or v_window > 365 then
        raise exception using errcode = '22023', message = 'flow_audience_form_window_invalid';
    end if;

    with purchases as (
        select p.funnel_ref, p.phone from public._flow_audience_purchases(v_tenant) p
         where p.phone ~ '^[1-9][0-9]{7,14}$'
    ),
    seeds as (
        -- prospecta: el formulario con permiso de WhatsApp del producto del flujo, en la ventana.
        select intent.normalized_phone as phone, max(intent.submitted_at) as form_at
          from public.purchase_intents intent
         where v_status = 'prospecta' and intent.tenant_ref = v_tenant and intent.funnel_ref = v_product
           and intent.whatsapp_contact_authorized
           and intent.submitted_at >= clock_timestamp() - make_interval(days => v_window)
         group by intent.normalized_phone
        union all
        -- clienta: quien compro el producto del flujo (sin reembolso ni contracargo). El validador exige
        -- para.producto igual al producto del flujo: «compro» es lo mismo en la lista, el armado y la admision.
        select purchase.phone, null::timestamptz
          from purchases purchase
         where v_status = 'clienta'
           and purchase.funnel_ref = v_product
         group by purchase.phone
    ),
    people as (
        select seed.phone, seed.form_at, public._whatsapp_phone_variants(seed.phone) as variants
          from seeds seed
         order by seed.form_at desc nulls last, seed.phone
         limit p_limit
    ),
    resolved as (
        select person.phone, person.form_at, person.variants,
               array(select distinct x.contact_id from (
                   select identity.contact_id from public.channel_identities identity
                    where identity.channel = 'whatsapp' and identity.external_user_id = any(person.variants)
                   union
                   select point.contact_id from public.contact_points point
                    where point.type = 'phone'
                      and regexp_replace(point.normalized_value, '[^0-9]', '', 'g') = any(person.variants)
               ) x) as contacts
          from people person
    )
    select jsonb_build_object(
        'candidates', coalesce(jsonb_agg(jsonb_build_object(
            'contact_id', r.contacts[1],
            'phone', r.phone,
            'form_at', r.form_at,
            'opted_out', exists (
                select 1 from public.contact_authorizations ca
                 where ca.contact_id = r.contacts[1] and ca.channel = 'whatsapp' and ca.authorization_status = 'denied'
                   and ca.valid_from <= clock_timestamp()
                   and (ca.valid_until is null or ca.valid_until > clock_timestamp())),
            'customer_products', coalesce((
                select jsonb_agg(distinct purchase.funnel_ref) from purchases purchase
                 where purchase.phone = any(r.variants)), '[]'::jsonb)
        ) order by r.form_at desc nulls last, r.phone) filter (where cardinality(r.contacts) = 1), '[]'::jsonb),
        'sin_contacto', count(*) filter (where cardinality(r.contacts) = 0),
        'ambiguas', count(*) filter (where cardinality(r.contacts) > 1))
      into v_result
      from resolved r;
    return v_result;
end;
$function$;

create function public.materialize_flow_audience_occurrence_v1(
    p_instance jsonb, p_flow_id uuid, p_version integer, p_occurrence jsonb, p_members jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_state text;
    v_tenant text;
    v_product text;
    v_definition jsonb;
    v_key text := p_occurrence ->> 'key';
    v_form text;
    v_existing public.flow_audience_occurrences%rowtype;
    v_id uuid;
    v_permission text;
    v_window integer;
    v_again text;
    v_repeat interval;
    v_member jsonb;
    v_position integer := 0;
    v_contact uuid;
    v_phone text;
    v_reason text;
    v_count integer := 0;
    v_omitted integer := 0;
    v_per_hour integer;
    v_per_occurrence integer;
begin
    if jsonb_typeof(p_occurrence) is distinct from 'object' or jsonb_typeof(p_members) is distinct from 'array'
       or v_key is null or p_version is null or p_flow_id is null then
        raise exception using errcode = '22023', message = 'flow_audience_occurrence_invalid';
    end if;
    select a.o_state, a.o_tenant_ref, a.o_product_ref, a.o_definition into v_state, v_tenant, v_product, v_definition
      from public._flow_audience_active(p_instance, p_flow_id, p_version) a;
    v_form := split_part(v_key, ':', 1);
    if v_form not in ('fecha', 'cada', 'manual') or public._flow_audience_form(v_definition) is distinct from v_form then
        raise exception using errcode = '22023', message = 'flow_audience_form_mismatch';
    end if;

    -- La idempotencia: la misma ocurrencia devuelve la que ya esta, sin tocarla.
    select * into v_existing from public.flow_audience_occurrences
     where flow_id = p_flow_id and flow_version = p_version and occurrence_key = v_key;
    if found then
        return jsonb_build_object('ok', true, 'created', false, 'occurrence_id', v_existing.id,
            'state', v_existing.state, 'member_count', v_existing.member_count,
            'omitted_count', v_existing.omitted_count);
    end if;
    if v_state not in ('prendido', 'cohorte') then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_not_running');
    end if;

    v_permission := v_definition #>> '{entra,permiso}';
    if v_permission not in ('formulario', 'compra') then
        raise exception using errcode = '22023', message = 'flow_audience_permission_invalid';
    end if;
    -- para.formulario_en: la misma regla que el validador (flow_audience.form_window_days) y la admision
    -- (20261112210600): de 1d a 365d, 30d sin la clave; otra cosa no es una ventana.
    v_window := case when coalesce(v_definition #>> '{para,formulario_en}', '30d') ~ '^[1-9][0-9]{0,2}d$'
                     then left(coalesce(v_definition #>> '{para,formulario_en}', '30d'), -1)::integer end;
    if v_window is null or v_window > 365 then
        raise exception using errcode = '22023', message = 'flow_audience_form_window_invalid';
    end if;
    v_per_hour := coalesce((v_definition #>> '{ritmo,por_hora}')::integer, 200);
    v_per_occurrence := coalesce((v_definition #>> '{ritmo,por_ocurrencia}')::integer, 1000);
    -- La repeticion (topes.repeticion.otra_despues_de; DEFAULT_REVISAR_DAN 7d): nunca, o una duracion.
    v_again := coalesce(v_definition #>> '{topes,repeticion,otra_despues_de}', '7d');
    v_repeat := case
        when v_again = 'nunca' then null
        when v_again ~ '^[1-9][0-9]{0,4}m$' then make_interval(mins => left(v_again, -1)::integer)
        when v_again ~ '^[1-9][0-9]{0,4}h$' then make_interval(hours => left(v_again, -1)::integer)
        when v_again ~ '^[1-9][0-9]{0,3}d$' then make_interval(days => left(v_again, -1)::integer)
        else interval '7 days'
    end;
    if jsonb_array_length(p_members) > v_per_occurrence then
        raise exception using errcode = '22023', message = 'flow_audience_over_cap';
    end if;
    if coalesce(p_occurrence ->> 'materialized_by', 'barredor') !~ '^(barredor|slack:[UW][A-Z0-9]{8,20})$' then
        raise exception using errcode = '22023', message = 'flow_audience_materializer_invalid';
    end if;

    insert into public.flow_audience_occurrences (
        flow_id, flow_version, occurrence_key, form, scheduled_at, audience_fingerprint, per_hour, per_occurrence,
        state, member_count, omitted_count, materialized_by, confirmed_by, confirmed_at)
    values (
        p_flow_id, p_version, v_key, v_form, (p_occurrence ->> 'scheduled_at')::timestamptz,
        p_occurrence ->> 'fingerprint', v_per_hour, v_per_occurrence,
        case when v_form = 'manual' then 'materializada' else 'confirmada' end, 0, 0,
        coalesce(p_occurrence ->> 'materialized_by', 'barredor'),
        case when v_form = 'manual' then null else 'publicacion' end,
        case when v_form = 'manual' then null else clock_timestamp() end)
    returning id into v_id;

    for v_member in select value from jsonb_array_elements(p_members) loop
        begin
            v_contact := (v_member ->> 'contact_id')::uuid;
        exception when invalid_text_representation then
            raise exception using errcode = '22023', message = 'flow_audience_member_invalid';
        end;
        v_phone := v_member ->> 'phone';
        if v_contact is null or v_phone is null or v_phone !~ '^[1-9][0-9]{7,14}$' then
            raise exception using errcode = '22023', message = 'flow_audience_member_invalid';
        end if;
        v_reason := null;
        if exists (select 1 from public.flow_audience_members m where m.occurrence_id = v_id and m.contact_id = v_contact) then
            continue;  -- repetida en la lista que mando el bridge: ya entro una vez
        elsif exists (
            select 1 from public.contact_authorizations ca
             where ca.contact_id = v_contact and ca.channel = 'whatsapp' and ca.authorization_status = 'denied'
               and ca.valid_from <= clock_timestamp() and (ca.valid_until is null or ca.valid_until > clock_timestamp())
        ) then
            v_reason := 'baja';
        elsif v_state = 'cohorte' and not exists (
            select 1 from public.flow_cohort_members cm where cm.flow_id = p_flow_id and cm.contact_id = v_contact
        ) then
            v_reason := 'fuera_de_cohorte';
        elsif v_permission = 'formulario' and not exists (
            select 1 from public.purchase_intents pi
             where pi.tenant_ref = v_tenant and pi.funnel_ref = v_product
               and pi.normalized_phone = any(public._whatsapp_phone_variants(v_phone))
               and pi.whatsapp_contact_authorized
               and pi.submitted_at >= clock_timestamp() - make_interval(days => v_window)
        ) then
            v_reason := 'sin_permiso';
        elsif v_permission = 'compra' and not exists (
            select 1 from public._flow_audience_purchases(v_tenant) p
             where p.funnel_ref = v_product
               and p.phone = any(public._whatsapp_phone_variants(v_phone))
        ) then
            v_reason := 'sin_permiso';
        elsif exists (
            select 1 from public.flow_audience_members m
              join public.flow_audience_occurrences o on o.id = m.occurrence_id
             where o.flow_id = p_flow_id and m.contact_id = v_contact and m.state = 'liberada'
               and (v_repeat is null or m.released_at > clock_timestamp() - v_repeat)
        ) then
            v_reason := 'repetida';
        end if;
        insert into public.flow_audience_members (occurrence_id, position, contact_id, normalized_phone, state, omit_reason)
        values (v_id, v_position, v_contact, v_phone,
                case when v_reason is null then 'pendiente' else 'omitida' end, v_reason);
        v_position := v_position + 1;
        if v_reason is null then v_count := v_count + 1; else v_omitted := v_omitted + 1; end if;
    end loop;

    update public.flow_audience_occurrences
       set member_count = v_count, omitted_count = v_omitted,
           state = case when v_count = 0 and state = 'confirmada' then 'terminada' else state end,
           finished_at = case when v_count = 0 and state = 'confirmada' then clock_timestamp() end
     where id = v_id;
    return jsonb_build_object('ok', true, 'created', true, 'occurrence_id', v_id,
        'state', (select state from public.flow_audience_occurrences where id = v_id),
        'member_count', v_count, 'omitted_count', v_omitted);
end;
$function$;

create function public.brake_flow_audience_occurrence_v1(p_occurrence_id uuid, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_state text;
begin
    if p_reason is null or p_reason !~ '^[a-z][a-z0-9_:]{0,63}$' then
        raise exception using errcode = '22023', message = 'flow_audience_brake_reason_invalid';
    end if;
    select state into v_state from public.flow_audience_occurrences where id = p_occurrence_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_audience_occurrence_not_found';
    end if;
    if v_state not in ('materializada', 'confirmada', 'enviando') then
        return jsonb_build_object('ok', false, 'reason_code', 'not_brakeable', 'state', v_state);
    end if;
    update public.flow_audience_occurrences
       set state = 'frenada', brake_reason = p_reason, braked_at = clock_timestamp()
     where id = p_occurrence_id;
    return jsonb_build_object('ok', true);
end;
$function$;

-- La ocurrencia termina cuando no queda nadie pendiente ni liberado sin asentar.
create function public._flow_audience_finish_if_done(p_occurrence_id uuid)
returns text
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_state text;
begin
    update public.flow_audience_occurrences o
       set state = 'terminada', finished_at = clock_timestamp()
     where o.id = p_occurrence_id and o.state in ('confirmada', 'enviando')
       and not exists (
           select 1 from public.flow_audience_members m
            where m.occurrence_id = o.id
              and (m.state = 'pendiente' or (m.state = 'liberada' and m.opened_at is null)));
    select state into v_state from public.flow_audience_occurrences where id = p_occurrence_id;
    return v_state;
end;
$function$;

create function public.release_flow_audience_members_v1(p_occurrence_id uuid, p_now timestamptz, p_limit integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_row public.flow_audience_occurrences%rowtype;
    v_recent integer;
    v_allowance integer;
    v_again jsonb;
    v_released jsonb;
    v_left integer;
begin
    if p_now is null or p_limit is null or p_limit < 1 or p_limit > 1000 then
        raise exception using errcode = '22023', message = 'flow_audience_release_invalid';
    end if;
    select * into v_row from public.flow_audience_occurrences where id = p_occurrence_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_audience_occurrence_not_found';
    end if;
    if v_row.state not in ('confirmada', 'enviando') then
        return jsonb_build_object('ok', false, 'reason_code', 'not_releasable', 'state', v_row.state);
    end if;
    -- Las liberadas que el motor no asento en 10 minutos vuelven a darse (el motor
    -- abre por evento_id unico: abrir dos veces devuelve el mismo recorrido).
    select coalesce(jsonb_agg(jsonb_build_object('position', m.position, 'contact_id', m.contact_id,
                                                 'phone', m.normalized_phone, 'again', true)
                              order by m.position), '[]'::jsonb)
      into v_again
      from (select position, contact_id, normalized_phone from public.flow_audience_members
             where occurrence_id = p_occurrence_id and state = 'liberada' and opened_at is null
               and released_at <= p_now - interval '10 minutes'
             order by position limit p_limit) m;
    select count(*) into v_recent from public.flow_audience_members
     where occurrence_id = p_occurrence_id and state = 'liberada'
       and released_at > p_now - interval '1 hour' and released_at <= p_now;
    v_allowance := least(p_limit - jsonb_array_length(v_again), greatest(v_row.per_hour - v_recent, 0));
    with chosen as (
        select position from public.flow_audience_members
         where occurrence_id = p_occurrence_id and state = 'pendiente'
         order by position limit greatest(v_allowance, 0)
    ), marked as (
        update public.flow_audience_members m set state = 'liberada', released_at = p_now
          from chosen where m.occurrence_id = p_occurrence_id and m.position = chosen.position
        returning m.position, m.contact_id, m.normalized_phone
    )
    select coalesce(jsonb_agg(jsonb_build_object('position', position, 'contact_id', contact_id,
                                                 'phone', normalized_phone, 'again', false)
                              order by position), '[]'::jsonb)
      into v_released from marked;
    select count(*) into v_left from public.flow_audience_members
     where occurrence_id = p_occurrence_id and state = 'pendiente';
    update public.flow_audience_occurrences set state = 'enviando'
     where id = p_occurrence_id and state = 'confirmada';
    return jsonb_build_object('ok', true, 'released', v_again || v_released, 'pending', v_left,
        'state', public._flow_audience_finish_if_done(p_occurrence_id),
        'per_hour_left', greatest(v_row.per_hour - v_recent - jsonb_array_length(v_released), 0));
end;
$function$;

create function public.settle_flow_audience_member_v1(p_occurrence_id uuid, p_contact_id uuid, p_outcome text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_settled boolean;
begin
    if p_outcome is null or p_outcome not in ('arranco', 'no_arranca', 'sin_flujo') or p_contact_id is null then
        raise exception using errcode = '22023', message = 'flow_audience_settle_invalid';
    end if;
    perform 1 from public.flow_audience_occurrences where id = p_occurrence_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_audience_occurrence_not_found';
    end if;
    update public.flow_audience_members set opened_at = clock_timestamp(), open_outcome = p_outcome
     where occurrence_id = p_occurrence_id and contact_id = p_contact_id and state = 'liberada' and opened_at is null;
    v_settled := found;
    return jsonb_build_object('ok', true, 'settled', v_settled,
        'state', public._flow_audience_finish_if_done(p_occurrence_id));
end;
$function$;

-- Las ocurrencias de un tenant y aliada, con sus conteos. Sin telefonos.
create function public._flow_audience_occurrence_rows(p_tenant_ref text, p_ally_ref text, p_limit integer)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select coalesce((
        select jsonb_agg(row_to_json(r)::jsonb order by r.materialized_at desc)
          from (
            select o.id as occurrence_id, o.flow_id, c.flow_key, c.product_ref, o.flow_version, o.occurrence_key,
                   o.form, o.state, o.scheduled_at, o.member_count, o.omitted_count, o.per_hour, o.per_occurrence,
                   o.brake_reason, o.materialized_by, o.materialized_at, o.confirmed_by, o.confirmed_at,
                   o.finished_at,
                   (select count(*) from public.flow_audience_members m
                     where m.occurrence_id = o.id and m.state = 'liberada')::integer as released_count,
                   (select count(*) from public.flow_audience_members m
                     where m.occurrence_id = o.id and m.open_outcome = 'arranco')::integer as started_count,
                   (select coalesce(jsonb_object_agg(x.omit_reason, x.n), '{}'::jsonb)
                      from (select m.omit_reason, count(*) as n from public.flow_audience_members m
                             where m.occurrence_id = o.id and m.state = 'omitida' group by m.omit_reason) x)
                       as omitted_by_reason
              from public.flow_audience_occurrences o
              join public.flow_controls c on c.flow_id = o.flow_id
             where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
             order by o.materialized_at desc
             limit p_limit
          ) r
    ), '[]'::jsonb)
$function$;

create function public.read_flow_audience_occurrences_v1(p_instance jsonb, p_limit integer)
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
        raise exception using errcode = '22023', message = 'flow_audience_read_invalid';
    end if;
    select i.o_tenant_ref, i.o_ally_ref into v_tenant, v_ally from public._flow_store_instance(p_instance) i;
    return public._flow_audience_occurrence_rows(v_tenant, v_ally, p_limit);
end;
$function$;

-- ===========================================================================
-- 3. Las funciones de la pagina (rol flow_change_approver, con la sesion).
-- ===========================================================================

-- Una ocurrencia de la instancia, bloqueada; o P0002.
create function public._flow_audience_occurrence_of(p_tenant_ref text, p_ally_ref text, p_occurrence_id uuid)
returns public.flow_audience_occurrences
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_row public.flow_audience_occurrences%rowtype;
begin
    select o.* into v_row from public.flow_audience_occurrences o
      join public.flow_controls c on c.flow_id = o.flow_id
     where o.id = p_occurrence_id and c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
       for update of o;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_audience_occurrence_not_found';
    end if;
    return v_row;
end;
$function$;

create function public.approver_read_flow_audience_v1(p_instance jsonb, p_session_hash text)
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
    return jsonb_build_object(
        'flows', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'flow_id', c.flow_id, 'flow_key', c.flow_key, 'product_ref', c.product_ref,
                       'state', c.state, 'version', c.active_version, 'nombre', d.definition ->> 'nombre',
                       'form', public._flow_audience_form(d.definition),
                       'para', d.definition -> 'para', 'ritmo', d.definition -> 'ritmo')
                     order by c.product_ref, c.flow_key)
              from public.flow_controls c
              join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
             where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
               and c.state in ('prendido', 'cohorte')
               and public._flow_audience_form(d.definition) is not null), '[]'::jsonb),
        'occurrences', public._flow_audience_occurrence_rows(v_instance.o_tenant_ref, v_instance.o_ally_ref, 50));
end;
$function$;

create function public.approver_confirm_flow_audience_v1(
    p_instance jsonb, p_session_hash text, p_occurrence_id uuid, p_expected_count integer, p_second_step boolean)
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
    v_row public.flow_audience_occurrences%rowtype;
    v_running text;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    v_row := public._flow_audience_occurrence_of(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_occurrence_id);
    if v_row.state <> 'materializada' then
        return jsonb_build_object('ok', false, 'reason_code', 'not_waiting_for_confirmation', 'state', v_row.state);
    end if;
    if p_second_step is distinct from true then
        return jsonb_build_object('ok', false, 'reason_code', 'second_step_missing');
    end if;
    if p_expected_count is distinct from v_row.member_count then
        return jsonb_build_object('ok', false, 'reason_code', 'count_mismatch', 'member_count', v_row.member_count);
    end if;
    -- El flujo tiene que seguir corriendo con la misma version que armo la lista.
    select c.state into v_running from public.flow_controls c
     where c.flow_id = v_row.flow_id and c.active_version = v_row.flow_version and c.state in ('prendido', 'cohorte');
    if not found then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_not_running');
    end if;
    update public.flow_audience_occurrences
       set state = case when member_count = 0 then 'terminada' else 'confirmada' end,
           confirmed_by = 'slack:' || v_actor, confirmed_at = clock_timestamp(),
           finished_at = case when member_count = 0 then clock_timestamp() end
     where id = p_occurrence_id;
    return jsonb_build_object('ok', true, 'member_count', v_row.member_count);
end;
$function$;

create function public.approver_brake_flow_audience_v1(p_instance jsonb, p_session_hash text, p_occurrence_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_row public.flow_audience_occurrences%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    v_row := public._flow_audience_occurrence_of(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_occurrence_id);
    if v_row.state not in ('materializada', 'confirmada', 'enviando') then
        return jsonb_build_object('ok', false, 'reason_code', 'not_brakeable', 'state', v_row.state);
    end if;
    update public.flow_audience_occurrences
       set state = 'frenada', brake_reason = 'a_mano', braked_at = clock_timestamp()
     where id = p_occurrence_id;
    return jsonb_build_object('ok', true);
end;
$function$;

-- ===========================================================================
-- 4. Los permisos: tablas sin acceso por la API; las del bridge, solo
--    service_role; las de la pagina, solo flow_change_approver; las internas,
--    para nadie.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_table text;
    v_role text;
begin
    foreach v_table in array array['public.flow_audience_occurrences', 'public.flow_audience_members'] loop
        execute format('revoke all on table %s from public', v_table);
        for v_role in
            select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by 1
        loop
            execute format('revoke all on table %s from %I', v_table, v_role);
        end loop;
    end loop;
    foreach v_function in array array[
        'public._flow_audience_form(jsonb)',
        'public._flow_audience_active(jsonb,uuid,integer)',
        'public._flow_audience_purchases(text)',
        'public._flow_audience_finish_if_done(uuid)',
        'public._flow_audience_occurrence_rows(text,text,integer)',
        'public._flow_audience_occurrence_of(text,text,uuid)',
        'public.read_flow_audience_flows_v1(jsonb)',
        'public.read_flow_audience_candidates_v1(jsonb,uuid,integer,integer)',
        'public.materialize_flow_audience_occurrence_v1(jsonb,uuid,integer,jsonb,jsonb)',
        'public.brake_flow_audience_occurrence_v1(uuid,text)',
        'public.release_flow_audience_members_v1(uuid,timestamptz,integer)',
        'public.settle_flow_audience_member_v1(uuid,uuid,text)',
        'public.read_flow_audience_occurrences_v1(jsonb,integer)',
        'public.approver_read_flow_audience_v1(jsonb,text)',
        'public.approver_confirm_flow_audience_v1(jsonb,text,uuid,integer,boolean)',
        'public.approver_brake_flow_audience_v1(jsonb,text,uuid)'
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
        grant execute on function public.read_flow_audience_flows_v1(jsonb) to service_role;
        grant execute on function public.read_flow_audience_candidates_v1(jsonb, uuid, integer, integer) to service_role;
        grant execute on function public.materialize_flow_audience_occurrence_v1(jsonb, uuid, integer, jsonb, jsonb)
            to service_role;
        grant execute on function public.brake_flow_audience_occurrence_v1(uuid, text) to service_role;
        grant execute on function public.release_flow_audience_members_v1(uuid, timestamptz, integer) to service_role;
        grant execute on function public.settle_flow_audience_member_v1(uuid, uuid, text) to service_role;
        grant execute on function public.read_flow_audience_occurrences_v1(jsonb, integer) to service_role;
    end if;
    grant execute on function public.approver_read_flow_audience_v1(jsonb, text) to flow_change_approver;
    grant execute on function public.approver_confirm_flow_audience_v1(jsonb, text, uuid, integer, boolean)
        to flow_change_approver;
    grant execute on function public.approver_brake_flow_audience_v1(jsonb, text, uuid) to flow_change_approver;
end;
$acl$;

commit;
