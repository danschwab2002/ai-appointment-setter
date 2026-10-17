-- Migration: lo que hizo la persona, para la entrada del agente, en el runtime
-- con manifiesto. Paso 2b de varios productos por aliada.
--
-- Por que. Decision de Dan del 2026-10-16 «el agente recibe todo el
-- conocimiento y decide el producto»: el agente infiere de que producto se
-- habla con la conversacion y con lo que hizo la persona, que le pasa el
-- bridge: que formularios, carritos, pagos o compras dejo, de que producto y
-- cuando. Hasta aca lo unico que le llegaba era el ultimo pago rechazado de la
-- semana (get_portable_recent_payment_failure_v1, 20261012000100).
--
-- Que crea. Una funcion nueva, ninguna tabla:
--
-- get_portable_person_activity_v1(tenant, funnel, telefono): lo que hizo ese
-- movil (por sus dos formas, 52/521 y 54/549, con _whatsapp_phone_variants de
-- 20261001000100) en los ultimos 30 dias, en la aliada y el funnel del pedido.
-- Una fila por cosa, la actividad y la hora, las 20 mas recientes, de la mas
-- vieja a la mas nueva:
--
-- - form: cada envio del formulario (purchase_intent_submissions, con la hora
--   del envio), de las intenciones del funnel;
-- - cart, payment_rejected, payment_slip_printed, payment_slip_expired: los
--   eventos de Hotmart que la admision portable ato al funnel
--   (commercial_ally_hotmart_event_bindings);
-- - purchase: las compras aprobadas que paso el correlador portable
--   (portable_hotmart_purchase_correlations), con cualquier resultado, porque
--   la compra es del producto del funnel aunque no se cruce con un formulario.
--
-- El telefono de un evento sale de data.buyer.checkout_phone o, si no viene,
-- de data.buyer.phone: medido en att1-db el 2026-10-16, los carritos lo traen
-- en phone (294 de 299; 5 sin ninguno) y los pagos y las compras en
-- checkout_phone. Nada de email, nombre, documento, transaccion ni payload.
-- Solo lee.
--
-- El funnel es el producto: con un funnel_ref por producto (decision del
-- 2026-10-15), el bridge le pone a cada fila la clave del producto de ese
-- funnel.

begin;

create or replace function public.get_portable_person_activity_v1(
    p_tenant_ref text,
    p_funnel_ref text,
    p_whatsapp_phone text
) returns table (
    activity text,
    occurred_at timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_variants text[];
    v_since timestamptz;
begin
    if nullif(btrim(p_tenant_ref), '') is null
       or nullif(btrim(p_funnel_ref), '') is null
       or p_whatsapp_phone is null
       or p_whatsapp_phone !~ '^[1-9][0-9]{7,14}$' then
        raise exception using errcode = '22023',
            message = 'invalid_portable_person_activity_input';
    end if;
    v_variants := public._whatsapp_phone_variants(p_whatsapp_phone);
    v_since := clock_timestamp() - interval '30 days';

    -- person_activity: begin
    return query
    with everything as (
        select 'form'::text as activity, submission.received_at as occurred_at
          from public.purchase_intents intent
          join public.purchase_intent_submissions link
            on link.purchase_intent_id = intent.id
          join public.precheckout_submissions submission
            on submission.id = link.submission_id
         where intent.tenant_ref = p_tenant_ref
           and intent.funnel_ref = p_funnel_ref
           and intent.normalized_phone = any(v_variants)
           and submission.received_at >= v_since
        union all
        select case event.event_type
                    when 'PURCHASE_OUT_OF_SHOPPING_CART' then 'cart'
                    when 'PURCHASE_CANCELED' then 'payment_rejected'
                    when 'PURCHASE_BILLET_PRINTED' then 'payment_slip_printed'
                    when 'PURCHASE_EXPIRED' then 'payment_slip_expired'
               end,
               event.received_at
          from public.webhook_events event
          join public.commercial_ally_hotmart_event_bindings provenance
            on provenance.webhook_event_id = event.id
         where event.source = 'hotmart'
           and event.event_type in (
               'PURCHASE_OUT_OF_SHOPPING_CART',
               'PURCHASE_CANCELED',
               'PURCHASE_BILLET_PRINTED',
               'PURCHASE_EXPIRED'
           )
           and provenance.tenant_ref = p_tenant_ref
           and provenance.funnel_ref = p_funnel_ref
           and event.received_at >= v_since
           and nullif(regexp_replace(coalesce(
                   event.payload #>> '{data,buyer,checkout_phone}',
                   event.payload #>> '{data,buyer,phone}',
                   ''
               ), '[^0-9]', '', 'g'), '') = any(v_variants)
        union all
        select 'purchase', event.received_at
          from public.webhook_events event
          join public.portable_hotmart_purchase_correlations correlation
            on correlation.webhook_event_id = event.id
         where event.source = 'hotmart'
           and event.event_type = 'PURCHASE_APPROVED'
           and correlation.tenant_ref = p_tenant_ref
           and correlation.funnel_ref = p_funnel_ref
           and event.received_at >= v_since
           and nullif(regexp_replace(coalesce(
                   event.payload #>> '{data,buyer,checkout_phone}',
                   event.payload #>> '{data,buyer,phone}',
                   ''
               ), '[^0-9]', '', 'g'), '') = any(v_variants)
    ),
    latest as (
        select everything.activity, everything.occurred_at
          from everything
         order by everything.occurred_at desc
         limit 20
    )
    select latest.activity, latest.occurred_at
      from latest
     order by latest.occurred_at asc;
    -- person_activity: end
end;
$function$;

-- Supabase le da execute por defecto a toda funcion nueva: se revoca a todos y
-- queda solo para service_role, el rol del bridge.
do $roles$
declare
    v_signature text := 'public.get_portable_person_activity_v1(text,text,text)';
begin
    execute format('revoke all on function %s from public', v_signature);
    if to_regrole('anon') is not null then
        execute format('revoke all on function %s from anon', v_signature);
    end if;
    if to_regrole('authenticated') is not null then
        execute format('revoke all on function %s from authenticated', v_signature);
    end if;
    if to_regrole('service_role') is not null then
        execute format('revoke all on function %s from service_role', v_signature);
        execute format('grant execute on function %s to service_role', v_signature);
    end if;

    -- Por presencia: service_role la ejecuta y ningun rol de la API.
    if exists (
           select 1 from pg_roles
           where rolname = 'service_role'
             and not has_function_privilege(rolname, v_signature, 'EXECUTE')
       )
       or exists (
           select 1 from pg_roles
           where rolname in ('anon', 'authenticated')
             and has_function_privilege(rolname, v_signature, 'EXECUTE')
       ) then
        raise exception using errcode = '55000',
            message = 'portable_person_activity_acl_unexpected',
            detail = format('solo service_role ejecuta %s', v_signature);
    end if;
end
$roles$;

commit;
