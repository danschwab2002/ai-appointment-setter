-- customer-status-v1: si la persona de una conversacion ya es cliente, y de que
-- producto (E-297, P-11; diseno E-278, PR 1; decisiones de Dan del 02/11, D1 a
-- D3). Contrato: docs/contracts/customer-status-v1.md.
--
-- Por que. Hoy el agente no sabe que habla con una clienta. person_activity
-- (get_portable_person_activity_v1, 20261016000200/0300) viaja solo con
-- [[productos]] en el manifiesto, que ni ATT1 ni Maribel declaran, y aunque
-- viajara busca solo por el telefono del WhatsApp, mira 30 dias y solo el
-- funnel de cada producto. Lo medido (E-037, E-092): en ATT1 el equipo les
-- mando reactivaciones y links a mano a tres clientas, y en Maribel las
-- clientas son el 14,4 % de las conversaciones con respuesta.
--
-- Que suma. Una sola funcion de lectura, get_customer_status_v1, solo para
-- service_role. Devuelve una fila por funnel (= producto) de la aliada en el
-- que la persona compro, con:
--   * status: 'cliente'; 'reembolsada' si todas sus compras de ese producto
--     tienen despues un PURCHASE_REFUNDED; 'contracargo' si alguna tiene un
--     PURCHASE_CHARGEBACK o PURCHASE_PROTEST y no le queda ninguna compra
--     vigente. Hoy el bridge no guarda esos eventos (hotmart.py los descarta
--     como unsupported_event_type), asi que todo sale 'cliente' hasta el PR que
--     los acepta (D4); la funcion ya los lee por la transaccion de la compra,
--     para que ese PR no necesite otra migracion;
--   * first_purchase_at y last_purchase_at, la hora en que llego cada compra
--     vigente (webhook_events.received_at, como person_activity);
--   * source, la fuente mas segura que la ata a la persona, en este orden (D2):
--     'link' (una emision de esta conversacion que termino purchase_matched),
--     'telefono' (el telefono de la compra, o la identidad a la que se mando
--     el link, por las dos formas del movil, 52/521 y 54/549) y 'mail' (el
--     mail de la compra contra los mails del contacto de este WhatsApp:
--     contact_points de tipo email y contacts.email). La cuarta fuente de D2,
--     la etiqueta 'cliente' puesta a mano en Chatwoot, la suma el bridge: vive
--     en la conversacion, no en la base.
-- Sin ventana de tiempo: ser clienta no vence (D3). Las compras que cuenta son
-- los PURCHASE_APPROVED de los funnels pedidos, por su correlacion portable
-- (cualquier resultado: la fila existe aunque la compra no se haya atado a una
-- intencion) o por una emision purchase_matched. No devuelve mail, telefono,
-- nombre, transaccion ni payload.
--
-- Solo lectura: no cambia ninguna tabla, funcion, permiso ni fila. Correrla dos
-- veces aborta con 55000 sin cambiar nada. Errores: 22023 entrada invalida.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._whatsapp_phone_variants(text)') is null
       or to_regclass('public.portable_hotmart_purchase_correlations') is null
       or to_regclass('public.checkout_link_issuances') is null
       or to_regclass('public.contact_points') is null then
        raise exception using errcode = '55000', message = 'customer_status_prerequisite_missing';
    end if;
    if to_regprocedure(
           'public.get_customer_status_v1(text,text[],bigint,bigint,bigint,text)'
       ) is not null then
        raise exception using errcode = '55000', message = 'customer_status_already_applied';
    end if;
end;
$already$;

create function public.get_customer_status_v1(
    p_tenant_ref text,
    p_funnel_refs text[],
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_whatsapp_phone text
) returns table (
    funnel_ref text,
    status text,
    first_purchase_at timestamptz,
    last_purchase_at timestamptz,
    source text
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_variants text[];
begin
    if nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_refs is null
       or cardinality(p_funnel_refs) = 0
       or cardinality(p_funnel_refs) > 20
       or exists (
           select 1 from unnest(p_funnel_refs) wanted(ref)
            where nullif(btrim(wanted.ref), '') is null
       )
       or p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id <= 0
       or p_whatsapp_phone is null
       or p_whatsapp_phone !~ '^[1-9][0-9]{7,14}$' then
        raise exception using errcode = '22023',
            message = 'invalid_customer_status_input';
    end if;
    v_variants := public._whatsapp_phone_variants(p_whatsapp_phone);

    -- customer_status: begin
    return query
    with purchases as (
        -- Toda compra aprobada de un funnel pedido: por su correlacion portable,
        -- que la admision escribe para cada compra del producto (resuelta o no)...
        select correlation.funnel_ref, event.id as event_id, event.received_at,
               event.payload
          from public.webhook_events event
          join public.portable_hotmart_purchase_correlations correlation
            on correlation.webhook_event_id = event.id
         where event.source = 'hotmart'
           and event.event_type = 'PURCHASE_APPROVED'
           and correlation.tenant_ref = p_tenant_ref
           and correlation.funnel_ref = any(p_funnel_refs)
        union
        -- ... o por nuestro link, que va por la admision de la emision y no
        -- escribe la correlacion portable (20261016000300).
        select intent.funnel_ref, event.id, event.received_at, event.payload
          from public.checkout_link_issuances issuance
          join public.purchase_intents intent
            on intent.id = issuance.purchase_intent_id
          join public.webhook_events event
            on event.id = issuance.hotmart_webhook_event_id
         where issuance.status = 'purchase_matched'
           and intent.tenant_ref = p_tenant_ref
           and intent.funnel_ref = any(p_funnel_refs)
           and event.source = 'hotmart'
           and event.event_type = 'PURCHASE_APPROVED'
    ),
    this_conversation_links as (
        select issuance.hotmart_webhook_event_id as event_id
          from public.checkout_link_issuances issuance
         where issuance.status = 'purchase_matched'
           and issuance.chatwoot_account_id = p_chatwoot_account_id
           and issuance.chatwoot_inbox_id = p_chatwoot_inbox_id
           and issuance.chatwoot_conversation_id = p_chatwoot_conversation_id
           and issuance.hotmart_webhook_event_id is not null
    ),
    links_to_this_phone as (
        select issuance.hotmart_webhook_event_id as event_id
          from public.checkout_link_issuances issuance
          join public.channel_identities identity
            on identity.id = issuance.channel_identity_id
         where issuance.status = 'purchase_matched'
           and issuance.hotmart_webhook_event_id is not null
           and identity.external_user_id = any(v_variants)
    ),
    person_contacts as (
        select identity.contact_id
          from public.channel_identities identity
         where identity.channel = 'whatsapp'
           and identity.external_user_id = any(v_variants)
        union
        select point.contact_id
          from public.contact_points point
         where point.type = 'phone'
           and regexp_replace(point.normalized_value, '[^0-9]', '', 'g') = any(v_variants)
    ),
    person_emails as (
        select lower(btrim(point.normalized_value)) as email
          from public.contact_points point
          join person_contacts person on person.contact_id = point.contact_id
         where point.type = 'email'
        union
        select lower(btrim(contact.email))
          from public.contacts contact
          join person_contacts person on person.contact_id = contact.id
         where nullif(btrim(contact.email), '') is not null
    ),
    matched as (
        select purchase.funnel_ref, purchase.received_at,
               case
                   when purchase.event_id in (select event_id from this_conversation_links) then 1
                   when purchase.event_id in (select event_id from links_to_this_phone)
                        or nullif(regexp_replace(coalesce(
                               purchase.payload #>> '{data,buyer,checkout_phone}',
                               purchase.payload #>> '{data,buyer,phone}',
                               ''
                           ), '[^0-9]', '', 'g'), '') = any(v_variants) then 2
                   when lower(btrim(purchase.payload #>> '{data,buyer,email}'))
                        in (select email from person_emails) then 3
               end as source_rank,
               (
                   select max(case reversal.event_type
                                  when 'PURCHASE_REFUNDED' then 1
                                  else 2
                              end)
                     from public.webhook_events reversal
                    where reversal.source = 'hotmart'
                      and reversal.event_type in (
                          'PURCHASE_REFUNDED', 'PURCHASE_CHARGEBACK', 'PURCHASE_PROTEST'
                      )
                      and nullif(purchase.payload #>> '{data,purchase,transaction}', '') is not null
                      and reversal.payload #>> '{data,purchase,transaction}'
                          = purchase.payload #>> '{data,purchase,transaction}'
               ) as reversal_rank
          from purchases purchase
    ),
    per_funnel as (
        select matched.funnel_ref,
               bool_or(matched.reversal_rank is null) as has_live_purchase,
               max(matched.reversal_rank) as worst_reversal,
               min(matched.received_at) filter (where matched.reversal_rank is null) as live_first,
               max(matched.received_at) filter (where matched.reversal_rank is null) as live_last,
               min(matched.received_at) as any_first,
               max(matched.received_at) as any_last,
               min(matched.source_rank) filter (where matched.reversal_rank is null) as live_source,
               min(matched.source_rank) as any_source
          from matched
         where matched.source_rank is not null
         group by matched.funnel_ref
    )
    select per_funnel.funnel_ref,
           case
               when per_funnel.has_live_purchase then 'cliente'
               when per_funnel.worst_reversal = 2 then 'contracargo'
               else 'reembolsada'
           end,
           coalesce(per_funnel.live_first, per_funnel.any_first),
           coalesce(per_funnel.live_last, per_funnel.any_last),
           case coalesce(per_funnel.live_source, per_funnel.any_source)
               when 1 then 'link'
               when 2 then 'telefono'
               else 'mail'
           end
      from per_funnel
     order by per_funnel.funnel_ref;
    -- customer_status: end
end;
$function$;

-- Los permisos: solo service_role (el bridge). Los privilegios por defecto de
-- Supabase le dan EXECUTE a anon y authenticated sobre toda funcion nueva: se
-- revocan explicitamente y se verifica por presencia.
do $acl$
declare
    v_signature text := 'public.get_customer_status_v1(text,text[],bigint,bigint,bigint,text)';
    v_role text;
begin
    execute format('revoke all on function %s from public', v_signature);
    foreach v_role in array array['anon', 'authenticated'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function %s from %I', v_signature, v_role);
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        execute format('grant execute on function %s to service_role', v_signature);
    end if;
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
            message = 'customer_status_acl_unexpected',
            detail = format('solo service_role ejecuta %s', v_signature);
    end if;
end;
$acl$;

commit;
