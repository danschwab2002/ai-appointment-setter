-- read-results-v1: los numeros del tablero de resultados, leidos de la base de la
-- instancia (E-507, programa etapa-1; diseno en el OS,
-- coordinacion/etapa-1/diseno-monitoreo-instalable.md, paso R1).
--
-- Por que. El tablero de resultados de cada aliada lo arma hoy el OS de Dan con
-- un SQL propio (maribel_ventas_por_flujo.sql, la regla venta_recuperada_v1)
-- corrido por psql adentro del VPS. Una instancia nueva no tiene tablero hasta
-- que alguien lo instala a mano. Con esta lectura, el bridge contesta los mismos
-- numeros en /internal/automation-expert/v1/resultados, y el AE puede responder
-- «cuanto vendimos» con la misma vara que el tablero.
--
-- Que hace. Una sola funcion nueva, de solo lectura y solo para service_role:
--   read_results_v1(p_desde, p_hasta, p_tz, p_pruebas, p_ventana_dias, p_piso_min)
-- que devuelve un jsonb con:
--   ventana  los parametros con que se midio;
--   flujos   por flujo de negocio y ejecutor (legado, motor, agente): envios
--            pedidos y aceptados, ventas recuperadas y por que via (link, caso,
--            identidad), en [p_desde, p_hasta);
--   compras  las compras aprobadas de la ventana partidas por via (incluye
--            prueba, devuelta y las que no son recuperadas);
--   dias     aceptados y ventas por dia (en p_tz), flujo y ejecutor;
--   ventas   cada venta recuperada: los caracteres 3 a 8 de su id (como la foto
--            del tablero), la fecha UTC, flujo, ejecutor, via, horas desde el
--            toque, tipo de pago, y de Hotmart el pais, la moneda, el precio y la
--            comision PRODUCER en USD;
--   campanas links y ventas por link por campana del AE y, sin campana, por
--            flujo del toque que mando el link.
-- La regla es la de venta_recuperada_v1, copiada del SQL del OS sin cambios de
-- criterio (la cabecera de ese archivo la explica entera). Las pruebas: las
-- cohortes (pilot_cohort_memberships, flow_cohort_members), los reciclados
-- (test_activity_ids_v1) y los contactos cuyo id empieza con algun prefijo de
-- p_pruebas, extendidos por email y por los ultimos 10 digitos del telefono.
--
-- Nada de una persona sale: ni email, ni telefono, ni nombre; las ventas van por
-- un pedazo de su id.
--
-- Lo que NO hace, a proposito: conversaciones de Chatwoot y costo del modelo (no
-- viven en esta base) ni costo de Meta (lo lee el bridge de la Graph API).
-- Ninguna tabla nueva, ningun permiso cambiado.

begin;

do $guard$
begin
    if to_regprocedure('public.read_results_v1(timestamptz,timestamptz,text,text[],integer,integer)') is not null then
        raise exception 'read_results_v1 ya existe: esta migracion corre una sola vez';
    end if;
end;
$guard$;

create function public.read_results_v1(
    p_desde timestamptz,
    p_hasta timestamptz,
    p_tz text,
    p_pruebas text[],
    p_ventana_dias integer,
    p_piso_min integer
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_out jsonb;
begin
    if p_desde is null or p_hasta is null or p_desde >= p_hasta then
        raise exception 'read_results_v1: la ventana va con desde anterior a hasta' using errcode = '22023';
    end if;
    if p_hasta - p_desde > interval '400 days' then
        raise exception 'read_results_v1: la ventana pasa los 400 dias' using errcode = '22023';
    end if;
    if p_tz is null or not exists (select 1 from pg_timezone_names where name = p_tz) then
        raise exception 'read_results_v1: zona desconocida' using errcode = '22023';
    end if;
    if p_ventana_dias is null or p_ventana_dias not between 1 and 31
       or p_piso_min is null or p_piso_min not between 0 and 1440 then
        raise exception 'read_results_v1: ventana de atribucion o piso fuera de rango' using errcode = '22023';
    end if;
    if coalesce(cardinality(p_pruebas), 0) > 200
       or exists (select 1 from unnest(coalesce(p_pruebas, '{}'::text[])) pr
                   where pr is null or pr !~ '^[0-9a-f-]{4,36}$') then
        raise exception 'read_results_v1: los prefijos de prueba van en hexadecimal (4 a 36)' using errcode = '22023';
    end if;

    with
    p as materialized (
        select p_desde as desde, p_hasta as hasta, p_ventana_dias as vd,
               make_interval(mins => p_piso_min) as piso, p_tz as tz
    ),
    tai as materialized (
        select kind, id from public.test_activity_ids_v1()
    ),
    ident as materialized (
        select contact_id, 'e' as k, lower(btrim(normalized_value)) as v
          from public.contact_points where type = 'email'
        union
        select contact_id, 'p', right(regexp_replace(normalized_value, '\D', '', 'g'), 10)
          from public.contact_points
         where type = 'phone' and length(regexp_replace(normalized_value, '\D', '', 'g')) >= 10
        union
        select id, 'e', lower(btrim(email)) from public.contacts where nullif(btrim(email), '') is not null
        union
        select id, 'p', right(regexp_replace(phone, '\D', '', 'g'), 10)
          from public.contacts where length(regexp_replace(coalesce(phone, ''), '\D', '', 'g')) >= 10
        union
        select contact_id, 'p', right(regexp_replace(external_user_id, '\D', '', 'g'), 10)
          from public.channel_identities
         where channel = 'whatsapp' and length(regexp_replace(coalesce(external_user_id, ''), '\D', '', 'g')) >= 10
    ),
    semillas as (
        select c.id from public.contacts c
         where exists (select 1 from unnest(coalesce(p_pruebas, '{}'::text[])) pr
                        where c.id::text like pr || '%')
        union
        select contact_id from public.pilot_cohort_memberships
        union
        select contact_id from public.flow_cohort_members
        union
        select id from tai where kind = 'contact'
    ),
    ident_pruebas as materialized (
        select distinct i.k, i.v from ident i join semillas s on s.id = i.contact_id
    ),
    pruebas as materialized (
        select id as contact_id from semillas
        union
        select i.contact_id from ident i join ident_pruebas ip on ip.k = i.k and ip.v = i.v
    ),
    caso as materialized (
        select rc.id, rc.contact_id, rc.source,
               case when nr.recovery_case_id is not null then 'motor' else 'legado' end as ejecutor,
               nr.trigger_event, fr.flow_key
          from public.recovery_cases rc
          left join public.flow_engine_native_runs nr on nr.recovery_case_id = rc.id
          left join public.flow_engine_runs fr on fr.recovery_case_id = rc.id
         where rc.source <> 'simulator'
    ),
    toques_todos as (
        select 'accion:' || sa.id as ref, c.contact_id, c.ejecutor,
               case when c.ejecutor = 'motor' then
                        case c.trigger_event when 'compra' then 'bienvenida' when 'carrito' then 'carrito'
                             else coalesce(c.flow_key, 'motor') end
                    when sa.step_key like 'payment_failure%' then 'pago_rechazado'
                    when sa.step_key like 'payment_slip%' then 'ficha_oxxo'
                    when c.source = 'landing' then 'primer_contacto'
                    else 'carrito' end as flujo,
               (select min(a.request_started_at) from public.followup_delivery_attempts a
                 where a.action_id = sa.id) as pedido,
               (select min(a.accepted_at) from public.followup_delivery_attempts a
                 where a.action_id = sa.id and a.outcome = 'accepted_by_chatwoot') as aceptado,
               c.id as caso
          from public.scheduled_actions sa join caso c on c.id = sa.recovery_case_id
        union all
        select 'seguimiento:' || f.id, cv.contact_id, 'legado', 'seguimiento_cupon', f.created_at,
               case when f.status = 'sent' then coalesce(f.settled_at, f.created_at) end, null::uuid
          from public.conversation_followup_events f join public.conversations cv on cv.id = f.conversation_id
        union all
        select 'reactivacion:' || r.id, cv.contact_id, 'legado', 'reactivacion', r.created_at,
               case when r.status = 'sent' then coalesce(r.settled_at, r.created_at) end, null::uuid
          from public.conversation_reactivation_events r join public.conversations cv on cv.id = r.conversation_id
        union all
        select 'link:' || i.id, i.contact_id, 'agente', 'agente_conversacion', i.created_at,
               case when i.status in ('accepted_by_chatwoot', 'purchase_matched')
                    then coalesce(i.finalized_at, i.created_at) end, null::uuid
          from public.checkout_link_issuances i
         where i.recovery_case_id is null
           and not exists (select 1 from public.conversation_followup_events f where f.checkout_issuance_id = i.id)
    ),
    toques as materialized (
        select t.* from toques_todos t
         where t.contact_id is not null
           and not exists (select 1 from pruebas pr where pr.contact_id = t.contact_id)
           and not exists (select 1 from tai where tai.kind = 'recovery_case' and tai.id = t.caso)
    ),
    iss_toque as materialized (
        select i.id, i.issuance_ulid, i.hotmart_webhook_event_id,
               coalesce(
                   (select 'accion:' || a.action_id from public.followup_delivery_attempts a
                     where a.id = i.delivery_attempt_id),
                   (select 'seguimiento:' || f.id from public.conversation_followup_events f
                     where f.checkout_issuance_id = i.id),
                   case when i.recovery_case_id is null then 'link:' || i.id end,
                   (select 'accion:' || sa.id from public.scheduled_actions sa
                     where sa.recovery_case_id = i.recovery_case_id order by sa.created_at limit 1)
               ) as ref
          from public.checkout_link_issuances i
    ),
    productos as (
        select distinct b.hotmart_product_id::text as product_id
          from public.commercial_ally_runtime_bindings b where b.hotmart_product_id is not null
    ),
    compras_todas as (
        select distinct on (coalesce(e.payload #>> '{data,purchase,transaction}', e.id::text))
               e.id, e.received_at as t,
               e.payload #>> '{data,purchase,transaction}' as tx,
               nullif(lower(btrim(e.payload #>> '{data,buyer,email}')), '') as email,
               case when length(regexp_replace(coalesce(nullif(e.payload #>> '{data,buyer,checkout_phone}', ''),
                                                        e.payload #>> '{data,buyer,phone}', ''), '\D', '', 'g')) >= 10
                    then right(regexp_replace(coalesce(nullif(e.payload #>> '{data,buyer,checkout_phone}', ''),
                                                       e.payload #>> '{data,buyer,phone}', ''), '\D', '', 'g'), 10)
               end as tel,
               coalesce(e.payload #>> '{data,purchase,origin,sck}', '') as sck,
               coalesce(e.payload #>> '{data,purchase,payment,type}', '-') as pago
          from public.webhook_events e, p
         where e.source = 'hotmart' and e.event_type = 'PURCHASE_APPROVED'
           and e.received_at >= p.desde and e.received_at < p.hasta
           and (not exists (select 1 from productos)
                or e.payload #>> '{data,product,id}' in (select product_id from productos))
         order by coalesce(e.payload #>> '{data,purchase,transaction}', e.id::text), e.received_at
    ),
    compras as materialized (
        select c.*,
               exists (select 1 from ident_pruebas ip
                        where (ip.k = 'e' and ip.v = c.email) or (ip.k = 'p' and ip.v = c.tel))
               or exists (select 1 from tai join public.webhook_events w on w.id = tai.id
                           where tai.kind = 'webhook_event'
                             and (w.id = c.id
                                  or (c.tx is not null and w.payload #>> '{data,purchase,transaction}' = c.tx)))
               or exists (select 1 from tai join public.checkout_link_issuances i on i.id = tai.id
                           where tai.kind = 'checkout_link_issuance'
                             and (i.hotmart_webhook_event_id = c.id
                                  or (c.sck <> '' and position('hermes~v1~' || i.issuance_ulid in c.sck) > 0)))
                  as prueba,
               exists (select 1 from public.webhook_events d
                        where d.source = 'hotmart'
                          and d.event_type in ('PURCHASE_REFUNDED', 'PURCHASE_CHARGEBACK', 'PURCHASE_PROTEST')
                          and c.tx is not null and d.payload #>> '{data,purchase,transaction}' = c.tx) as devuelta
          from compras_todas c
    ),
    comprador as materialized (
        select distinct c.id as compra, i.contact_id
          from compras c join ident i on (i.k = 'e' and i.v = c.email) or (i.k = 'p' and i.v = c.tel)
    ),
    atrib as materialized (
        select c.*,
               pl.ref as ref_link, pl.iss as iss_link, pl.hay_link,
               (select t.ref from public.recovery_cases rc
                  join toques t on t.caso = rc.id
                 where rc.purchase_event_id = c.id and t.aceptado <= c.t - p.piso
                 order by t.aceptado desc limit 1) as ref_caso,
               (select t.ref from toques t join comprador cb on cb.contact_id = t.contact_id and cb.compra = c.id
                 where t.aceptado <= c.t - p.piso and t.aceptado > c.t - make_interval(days => p.vd)
                 order by t.aceptado desc limit 1) as ref_ident,
               (select t.ref from toques t join comprador cb on cb.contact_id = t.contact_id and cb.compra = c.id
                 where t.aceptado <= c.t and t.aceptado > c.t - p.piso
                 order by t.aceptado desc limit 1) as ref_bajo,
               exists (select 1 from toques t join comprador cb on cb.contact_id = t.contact_id and cb.compra = c.id
                        where t.aceptado is not null) as tocada
          from compras c cross join p
          left join lateral (
              select (select it.ref from iss_toque it
                       where it.hotmart_webhook_event_id = c.id
                          or (c.sck <> '' and position('hermes~v1~' || it.issuance_ulid in c.sck) > 0)
                       order by (it.hotmart_webhook_event_id = c.id) desc limit 1) as ref,
                     (select it.id from iss_toque it
                       where it.hotmart_webhook_event_id = c.id
                          or (c.sck <> '' and position('hermes~v1~' || it.issuance_ulid in c.sck) > 0)
                       order by (it.hotmart_webhook_event_id = c.id) desc limit 1) as iss,
                     position('hermes~v1~' in c.sck) > 0
                     or exists (select 1 from iss_toque it where it.hotmart_webhook_event_id = c.id) as hay_link
          ) pl on true
    ),
    ventas as materialized (
        select a.id, a.t, a.pago, a.iss_link,
               coalesce(a.ref_link, a.ref_caso, a.ref_ident, a.ref_bajo) as ref,
               case when a.prueba then 'prueba'
                    when a.devuelta then 'devuelta'
                    when a.ref_link is not null then 'link'
                    when a.ref_caso is not null then 'caso'
                    when a.ref_ident is not null then 'identidad'
                    when a.hay_link then 'link_sin_emision'
                    when a.ref_bajo is not null then 'bajo_piso'
                    when a.email is null and a.tel is null then 'sin_dato'
                    when a.tocada then 'fuera_de_ventana'
                    else 'no_tocada' end as via
          from atrib a
    ),
    ventas_flujo as materialized (
        select v.*, t.flujo, t.ejecutor, t.aceptado as toque_en
          from ventas v left join toques t on t.ref = v.ref
         where v.via in ('link', 'caso', 'identidad')
    ),
    claves as (
        select distinct flujo, ejecutor from toques where pedido is not null
        union select distinct flujo, ejecutor from ventas_flujo
    ),
    flujos as (
        select k.flujo, k.ejecutor,
               (select count(*) from toques t where t.flujo = k.flujo and t.ejecutor = k.ejecutor
                    and t.pedido >= p.desde and t.pedido < p.hasta) as envios,
               (select count(*) from toques t where t.flujo = k.flujo and t.ejecutor = k.ejecutor
                    and t.aceptado >= p.desde and t.aceptado < p.hasta) as aceptados,
               (select count(*) from ventas_flujo v where v.flujo = k.flujo and v.ejecutor = k.ejecutor) as ventas,
               (select count(*) from ventas_flujo v where v.flujo = k.flujo and v.ejecutor = k.ejecutor
                    and v.via = 'link') as por_link,
               (select count(*) from ventas_flujo v where v.flujo = k.flujo and v.ejecutor = k.ejecutor
                    and v.via = 'caso') as por_caso,
               (select count(*) from ventas_flujo v where v.flujo = k.flujo and v.ejecutor = k.ejecutor
                    and v.via = 'identidad') as por_identidad
          from claves k cross join p
    ),
    dias as (
        select d.dia, d.flujo, d.ejecutor, sum(d.aceptados)::integer as aceptados, sum(d.ventas)::integer as ventas
          from (
            select (t.aceptado at time zone p.tz)::date as dia, t.flujo, t.ejecutor, count(*) as aceptados, 0 as ventas
              from toques t cross join p where t.aceptado >= p.desde and t.aceptado < p.hasta group by 1, 2, 3
            union all
            select (v.t at time zone p.tz)::date, v.flujo, v.ejecutor, 0, count(*)
              from ventas_flujo v cross join p group by 1, 2, 3
          ) d
         group by d.dia, d.flujo, d.ejecutor
    ),
    iss_grupo as materialized (
        select i.id, i.created_at, to_jsonb(i) ->> 'campaign_ref' as campana,
               coalesce(t.flujo || '/' || t.ejecutor, 'sin_flujo') as flujo
          from public.checkout_link_issuances i
          left join iss_toque it on it.id = i.id
          left join toques_todos t on t.ref = it.ref
         where (i.contact_id is null or not exists (select 1 from pruebas pr where pr.contact_id = i.contact_id))
           and not exists (select 1 from tai where tai.kind = 'checkout_link_issuance' and tai.id = i.id)
    ),
    campanas as (
        select 'campana:' || ac.campaign_key as grupo, ac.status as estado,
               (select count(*) from iss_grupo g where g.campana = ac.campaign_key
                    and g.created_at >= p.desde and g.created_at < p.hasta) as links,
               (select count(*) from ventas v join iss_grupo g on g.id = v.iss_link
                 where v.via = 'link' and g.campana = ac.campaign_key) as ventas
          from public.agent_campaigns ac cross join p
        union all
        select 'flujo:' || g.flujo, '-',
               count(*) filter (where g.created_at >= p.desde and g.created_at < p.hasta),
               (select count(*) from ventas v join iss_grupo g2 on g2.id = v.iss_link
                 where v.via = 'link' and g2.campana is null and g2.flujo = g.flujo)
          from iss_grupo g cross join p where g.campana is null group by g.flujo, p.desde, p.hasta
    ),
    monto as (
        select v.id,
               e.payload #>> '{data,purchase,checkout_country,iso}' as pais,
               e.payload #>> '{data,purchase,price,currency_value}' as moneda,
               case when e.payload #>> '{data,purchase,price,value}' ~ '^-?[0-9]+(\.[0-9]+)?$'
                    then (e.payload #>> '{data,purchase,price,value}')::numeric end as precio,
               (select case when c ->> 'value' ~ '^-?[0-9]+(\.[0-9]+)?$' then (c ->> 'value')::numeric end
                  from jsonb_array_elements(case when jsonb_typeof(e.payload #> '{data,commissions}') = 'array'
                                                 then e.payload #> '{data,commissions}' else '[]'::jsonb end) c
                 where c ->> 'source' = 'PRODUCER' and c ->> 'currency_value' = 'USD' limit 1) as neto_usd
          from ventas_flujo v join public.webhook_events e on e.id = v.id
    )
    select jsonb_build_object(
        'ventana', (select jsonb_build_object(
            'desde', to_char(p.desde at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'hasta', to_char(p.hasta at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'tz', p.tz, 'ventana_dias', p.vd, 'piso_min', p_piso_min,
            'regla', 'venta_recuperada_v1') from p),
        'flujos', coalesce((select jsonb_agg(jsonb_build_object(
            'flujo', f.flujo, 'ejecutor', f.ejecutor, 'envios', f.envios, 'aceptados', f.aceptados,
            'ventas', f.ventas, 'por_link', f.por_link, 'por_caso', f.por_caso, 'por_identidad', f.por_identidad)
            order by f.flujo, f.ejecutor) from flujos f), '[]'::jsonb),
        'compras', coalesce((select jsonb_agg(jsonb_build_object('via', x.via, 'n', x.n) order by x.via)
            from (select v.via, count(*) as n from ventas v group by v.via) x), '[]'::jsonb),
        'dias', coalesce((select jsonb_agg(jsonb_build_object(
            'dia', to_char(d.dia, 'YYYY-MM-DD'), 'flujo', d.flujo, 'ejecutor', d.ejecutor,
            'aceptados', d.aceptados, 'ventas', d.ventas) order by d.dia, d.flujo, d.ejecutor) from dias d), '[]'::jsonb),
        'ventas', coalesce((select jsonb_agg(jsonb_build_object(
            'venta', substr(v.id::text, 3, 6),
            'momento', to_char(v.t at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'fecha', to_char(v.t at time zone 'UTC', 'YYYY-MM-DD'),
            'flujo', v.flujo, 'ejecutor', v.ejecutor, 'via', v.via,
            'horas_desde_toque', round((extract(epoch from (v.t - v.toque_en)) / 3600.0)::numeric, 1),
            'pago', v.pago, 'pais', m.pais, 'moneda', m.moneda, 'precio', m.precio, 'neto_usd', m.neto_usd)
            order by v.t, v.id) from ventas_flujo v left join monto m on m.id = v.id), '[]'::jsonb),
        'campanas', coalesce((select jsonb_agg(jsonb_build_object(
            'grupo', c.grupo, 'estado', c.estado, 'links', c.links, 'ventas', c.ventas) order by c.grupo)
            from campanas c where c.links > 0 or c.ventas > 0 or c.grupo like 'campana:%'), '[]'::jsonb)
    )
    into v_out;
    return v_out;
end;
$function$;

revoke all on function public.read_results_v1(timestamptz, timestamptz, text, text[], integer, integer) from public;

do $roles$
declare
    v_role text;
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function public.read_results_v1(timestamptz, timestamptz, text, text[], integer, integer) from %I', v_role);
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.read_results_v1(timestamptz, timestamptz, text, text[], integer, integer) to service_role;
    end if;
end;
$roles$;

commit;
