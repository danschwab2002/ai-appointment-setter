-- Migration: lo que hizo la persona cuenta tambien la compra hecha con nuestro
-- link. Paso 2c-0 de varios productos por aliada.
--
-- Por que. get_portable_person_activity_v1 (20261016000200, la 2b) lee las
-- compras de portable_hotmart_purchase_correlations, y la compra hecha con el
-- link del agente no pasa por ahi: el bridge la manda a
-- admit_and_correlate_hotmart_checkout_issuance_v2 (20260914000100), que marca
-- la intencion purchased y la emision purchase_matched
-- (correlate_hotmart_checkout_issuance_v2, 20260922000200) pero no escribe la
-- correlacion portable. Medido en att1-db el 2026-10-16: de 72 compras
-- aprobadas, las 2 con el marcador de nuestro link en el sck son las unicas sin
-- fila de correlacion portable. El agente veia sin compra justo a quien compro
-- con nuestro link, y la regla del SOUL comun para quien ya compro no se
-- disparaba.
--
-- Que cambia. Solo el cuerpo de get_portable_person_activity_v1, con la misma
-- firma, el mismo resultado y los mismos permisos. Suma una rama de 'purchase':
-- las emisiones purchase_matched cuya intencion es de la aliada y el funnel del
-- pedido, con la hora de la compra (webhook_events.received_at, como la rama de
-- la correlacion portable). La persona es la identidad a la que se mando el link
-- (channel_identities de la emision) o el telefono del evento, por las dos
-- formas del movil. Una compra que tambien tenga correlacion portable no se
-- cuenta dos veces. Nada de email, nombre, documento, transaccion ni payload.
-- Solo lee.

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
        union all
        -- person_activity: link purchase
        select 'purchase', event.received_at
          from public.checkout_link_issuances issuance
          join public.purchase_intents intent
            on intent.id = issuance.purchase_intent_id
          join public.channel_identities identity
            on identity.id = issuance.channel_identity_id
          join public.webhook_events event
            on event.id = issuance.hotmart_webhook_event_id
         where issuance.status = 'purchase_matched'
           and intent.tenant_ref = p_tenant_ref
           and intent.funnel_ref = p_funnel_ref
           and event.source = 'hotmart'
           and event.event_type = 'PURCHASE_APPROVED'
           and event.received_at >= v_since
           and (
               identity.external_user_id = any(v_variants)
               or nullif(regexp_replace(coalesce(
                      event.payload #>> '{data,buyer,checkout_phone}',
                      event.payload #>> '{data,buyer,phone}',
                      ''
                  ), '[^0-9]', '', 'g'), '') = any(v_variants)
           )
           and not exists (
               select 1
                 from public.portable_hotmart_purchase_correlations correlation
                where correlation.webhook_event_id = event.id
           )
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

-- create or replace conserva los permisos de la 2b. Se verifican por presencia:
-- service_role la ejecuta y ningun rol de la API.
do $roles$
declare
    v_signature text := 'public.get_portable_person_activity_v1(text,text,text)';
begin
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
