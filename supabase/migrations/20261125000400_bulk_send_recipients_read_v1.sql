-- bulk-send-recipients-read-v1: el registro por persona de un envio masivo, para el
-- reporte (encargo 10 del mapa «El masivo manejado por el Automation Expert», D10 del
-- PR #511).
--
-- Por que hace falta: bulk_send_report.build_report necesita, por persona, el estado
-- del registro del motor y la conversacion y el mensaje de Chatwoot de la plantilla,
-- y ninguna funcion lo devuelve: read_bulk_send_batch_v1 trae solo a quien no tiene
-- registro y read_bulk_send_v1 / approver_read_bulk_send_v1 traen conteos.
--
-- 1. Una funcion del bridge, solo de service_role:
--    * read_bulk_send_recipients_v1(p_send_id, p_after_position, p_limit): las
--      personas del envio despues de p_after_position (-1 para empezar), en el orden
--      de la lista, hasta p_limit (1 a 500). Son las de la lista sin exclusion mas las
--      que tienen registro (una vetada al revalidar no tiene exclusion en la lista, y
--      una excluida despues no deja de haber recibido). Cada una con su posicion, el
--      telefono (null si la lista se purgo), el estado del registro (reservado,
--      enviado, fallido, dudoso o vetado; pendiente si no tiene), la conversacion y el
--      mensaje de Chatwoot, el codigo de Meta, el cierre y la primera compra aprobada
--      sin reembolso ni contracargo de uno de los productos del envio
--      (bulk_sends.product_refs), por el telefono o por la huella del mail, desde el
--      cierre del envio de esa persona. La compra se fecha con received_at del evento
--      de Hotmart: es cuando nos enteramos.
-- 2. Una auxiliar, de nadie: _bulk_send_purchase_times, las compras de
--    _bulk_send_purchases (20261125000300, sin tocarla) con su hora.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente. Correrla dos veces
-- aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida, P0002 lo que no existe.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.bulk_send_recipients') is null
       or to_regclass('public.bulk_send_prepared') is null
       or to_regprocedure('public._bulk_send_fingerprint(text,text)') is null
       or to_regprocedure('public._whatsapp_phone_variants(text)') is null
       or to_regclass('public.portable_hotmart_purchase_correlations') is null then
        raise exception using errcode = '55000', message = 'bulk_send_recipients_read_prerequisite_missing';
    end if;
    if to_regprocedure('public.read_bulk_send_recipients_v1(uuid,integer,integer)') is not null then
        raise exception using errcode = '55000', message = 'bulk_send_recipients_read_already_applied';
    end if;
end;
$already$;

-- Las compras aprobadas de la instancia sin reembolso ni contracargo, con su hora: la regla de
-- _bulk_send_purchases (20261125000300) mas received_at. El mail no sale de aca: solo su huella.
create function public._bulk_send_purchase_times(p_tenant_ref text)
returns table (funnel_ref text, phone text, email_fingerprint text, purchased_at timestamptz)
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select correlation.funnel_ref,
           nullif(regexp_replace(coalesce(event.payload #>> '{data,buyer,checkout_phone}',
                                          event.payload #>> '{data,buyer,phone}', ''), '[^0-9]', '', 'g'), ''),
           case when nullif(btrim(event.payload #>> '{data,buyer,email}'), '') is null then null
                else public._bulk_send_fingerprint('email', lower(btrim(event.payload #>> '{data,buyer,email}'))) end,
           event.received_at
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

-- El registro por persona de un envio, con cursor por posicion. Con telefono: solo lo lee el bridge.
create function public.read_bulk_send_recipients_v1(p_send_id uuid, p_after_position integer, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_send public.bulk_sends%rowtype;
begin
    if p_send_id is null or p_after_position is null or p_after_position < -1
       or p_limit is null or p_limit < 1 or p_limit > 500 then
        raise exception using errcode = '22023', message = 'bulk_send_read_invalid';
    end if;
    select * into v_send from public.bulk_sends where id = p_send_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'bulk_send_not_found';
    end if;
    if v_send.list_id is null then
        return '[]'::jsonb;
    end if;
    return coalesce((
        with page as (
            select m.position, m.phone, m.email_fingerprint,
                   case when m.phone is null then null else public._whatsapp_phone_variants(m.phone) end as variants,
                   r.state, r.chatwoot_conversation_id,
                   r.chatwoot_message_id, r.meta_error_code, r.closed_at
              from public.bulk_list_members m
              left join public.bulk_send_recipients r on r.send_id = p_send_id and r.position = m.position
             where m.list_id = v_send.list_id and m.position > p_after_position
               and (m.exclusion_reason is null or r.position is not null)
             order by m.position
             limit p_limit
        ),
        purchases as (
            select p.phone, p.email_fingerprint, p.purchased_at
              from public._bulk_send_purchase_times(v_send.tenant_ref) p
             where v_send.product_refs is not null and p.funnel_ref = any(v_send.product_refs)
        )
        select jsonb_agg(jsonb_build_object(
                   'position', x.position, 'phone', x.phone, 'state', coalesce(x.state, 'pendiente'),
                   'chatwoot_conversation_id', x.chatwoot_conversation_id,
                   'chatwoot_message_id', x.chatwoot_message_id, 'meta_error_code', x.meta_error_code,
                   'closed_at', x.closed_at,
                   'purchased_at', case when x.state = 'enviado' then (
                       select min(p.purchased_at) from purchases p
                        where p.purchased_at >= x.closed_at
                          and ((x.variants is not null and p.phone = any(x.variants))
                               or (x.email_fingerprint is not null and p.email_fingerprint = x.email_fingerprint)))
                   end)
                 order by x.position)
          from page x), '[]'::jsonb);
end;
$function$;

-- Los permisos: la lectura, solo service_role; la auxiliar, de nadie.
do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public.read_bulk_send_recipients_v1(uuid,integer,integer)',
        'public._bulk_send_purchase_times(text)'
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
        grant execute on function public.read_bulk_send_recipients_v1(uuid, integer, integer) to service_role;
    end if;
end;
$acl$;

commit;
