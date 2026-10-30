-- Migration: los dos frenos portables de la compra ven tambien la compra hecha
-- con nuestro link (E-215).
--
-- Por que. Una compra con el sck de una emision nuestra entra por
-- admit_and_correlate_hotmart_checkout_issuance_v2 (app.py): deja la emision
-- en purchase_matched (correlate_hotmart_checkout_issuance_v2, ultima
-- definicion 20261028000400) y no escribe portable_hotmart_purchase_correlations
-- ni hotmart_purchase_intent_event_identities. Los dos frenos portables la ven
-- solo de costado:
-- - _portable_precheckout_stop_reason (20261001000200): intent_purchased mira
--   solo la intencion del formulario que se esta por contactar, y
--   purchase_by_identity mira solo la correlacion portable. Quien compro con
--   nuestro link y vuelve a llenar el formulario tiene una intencion NUEVA en
--   waiting_for_purchase (el formulario busca solo intenciones esperando,
--   20260930000200), y su primer contacto sale. Es el hueco que encontro E-212
--   con las conversaciones 4 y 20 de Maribel.
-- - _portable_case_purchase_stop_reason (20261019000300): la intencion
--   purchased y el caso ganado cubren la compra por link solo si la intencion
--   de la emision cruza el contacto del caso, o si la emision tenia caso. Con
--   el comprador de Hotmart distinto de la intencion (otro mail, otro
--   telefono), o con una emision que se marco purchase_matched sin pasar su
--   intencion a purchased (20261028000400), el carrito nuevo de esa persona
--   no frena.
--
-- Que cambia:
-- 1. _portable_checkout_link_purchase_exists(tenant, funnel, contacto, mail,
--    telefono): la tercera fuente. Verdadero si hay una emision purchase_matched
--    cuya intencion es del mismo tenant y funnel (el mismo producto), y la
--    persona es la misma: la emision es de ese contacto, o el comprador que
--    informo Hotmart (_hotmart_purchase_intent_payload_identity del evento de
--    la compra, la misma identidad que guarda la admision portable) o la
--    intencion de la emision cruzan el mail exacto o el telefono en cualquiera
--    de sus formas (_whatsapp_phone_variants), contra el mail y el telefono
--    dados o contra los contact_points del contacto. D2: el contacto de la
--    emision frena aunque el link lo haya pagado otra persona (quien recibio
--    el link y no lo pago queda frenado para ese producto). Las identidades
--    de la persona se arman una sola vez. Solo lee, security
--    invoker, ningun rol de la API la ejecuta: la llaman los dos frenos, que
--    corren como su duenio.
-- 2. _portable_precheckout_stop_reason: despues de purchase_by_identity, con
--    el mismo motivo (purchase_by_identity), con la identidad de la intencion
--    y el contacto si lo hay.
-- 3. _portable_case_purchase_stop_reason: antes del null final, con el mismo
--    motivo (purchase_detected_any_offer), con el contacto del caso y el
--    telefono de su identidad de WhatsApp. Lo usa la reevaluacion del
--    carrito, del pago rechazado y de la ficha del legado, y la de los
--    recorridos nativos (el carrito y el pago rechazado nativos nacen con su
--    caso sobre el evento portable: 20261028000200, 20261030000200).
-- Los motivos no cambian: el plan del primer contacto, la reevaluacion y
-- quien los lee ya los conocen. Frena de mas antes que escribir de mas (D25).
--
-- Los reemplazos van sobre la definicion VIGENTE, con un texto exacto que
-- tiene que aparecer una sola vez y la marca que no puede estar (el metodo de
-- 20261019000300). Las vigentes: _portable_precheckout_stop_reason
-- 20261001000200, _portable_case_purchase_stop_reason 20261019000300.
--
-- 4. Un indice parcial de checkout_link_issuances (purchase_intent_id) para
--    las emisiones purchase_matched.
--
-- Lo que no hace: no toca el bridge, la admision de la compra por link, el
-- conteo de la venta (venta_recuperada_v1, 20261028000400) ni el RPC legado.
-- No cambia filas. Correrla dos veces aborta con 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $already$
begin
    if to_regprocedure('public._portable_checkout_link_purchase_exists(text,text,uuid,text,text)') is not null then
        raise exception using errcode = '55000',
            message = 'portable_checkout_link_purchase_stop_already_applied';
    end if;
end
$already$;

create function public._portable_checkout_link_purchase_exists(
    p_tenant_ref text,
    p_funnel_ref text,
    p_contact_id uuid,
    p_email text,
    p_phone text
)
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    -- portable_checkout_link_purchase_stop: una compra con nuestro link del
    -- producto, de la misma persona. Solo lee. Las identidades de la persona
    -- (el mail dado y los mails del contacto; las formas del telefono dado y
    -- de los telefonos del contacto) se arman una sola vez, y cada emision se
    -- compara contra esos dos conjuntos, sin una subconsulta por fila.
    with me as (
        select
            array_remove(
                array[p_email] || array(
                    select point.normalized_value
                    from public.contact_points point
                    where point.contact_id = p_contact_id
                      and point.type = 'email'
                ),
                null
            ) as emails,
            coalesce(public._whatsapp_phone_variants(p_phone), '{}'::text[])
            || array(
                select variant.value
                from public.contact_points point
                cross join lateral unnest(
                    public._whatsapp_phone_variants(point.normalized_value)
                ) variant (value)
                where point.contact_id = p_contact_id
                  and point.type = 'phone'
            ) as phones
    )
    select exists (
        select 1
        from me
        cross join public.checkout_link_issuances issuance
        join public.purchase_intents intent
          on intent.id = issuance.purchase_intent_id
        join public.webhook_events event
          on event.id = issuance.hotmart_webhook_event_id
        cross join lateral public._hotmart_purchase_intent_payload_identity(
            event.event_type, event.payload
        ) buyer
        cross join lateral (
            values (buyer.normalized_email, buyer.normalized_phone),
                   (intent.normalized_email, intent.normalized_phone)
        ) person (email, phone)
        where issuance.status = 'purchase_matched'
          and intent.tenant_ref = p_tenant_ref
          and intent.funnel_ref = p_funnel_ref
          and (
              -- D2: el contacto al que se le mando el link, aunque haya
              -- pagado otra persona.
              issuance.contact_id = p_contact_id
              or person.email = any(me.emails)
              or person.phone = any(me.phones)
          )
    )
$function$;

-- Las emisiones compradas, de un recorrido barato (decenas de filas por
-- instancia hoy; el indice parcial evita leer las que no se compraron).
create index checkout_link_issuances_purchase_matched_idx
    on public.checkout_link_issuances (purchase_intent_id)
    where status = 'purchase_matched';

revoke all on function public._portable_checkout_link_purchase_exists(text,text,uuid,text,text) from public;
do $grants$
declare
    v_role text;
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select 1 from pg_roles where rolname = v_role) then
            execute format(
                'revoke all on function public._portable_checkout_link_purchase_exists(text,text,uuid,text,text) from %I',
                v_role
            );
        end if;
    end loop;
end
$grants$;

-- Los dos frenos, sobre la definicion viva, con el texto contado.
do $stops_see_our_link$
declare
    v_precheckout constant text :=
        'public._portable_precheckout_stop_reason(uuid,uuid)';
    v_precheckout_anchor constant text :=
        E'        return ''purchase_by_identity'';\n'
        || E'    end if;\n';
    v_precheckout_block constant text :=
        v_precheckout_anchor
        || E'\n'
        || E'    -- portable_checkout_link_purchase_stop: la compra con nuestro link\n'
        || E'    -- (el sck de la emision) no escribe la correlacion portable.\n'
        || E'    if public._portable_checkout_link_purchase_exists(\n'
        || E'        v_binding.tenant_ref, v_binding.funnel_ref, p_contact_id,\n'
        || E'        v_intent.normalized_email, v_intent.normalized_phone\n'
        || E'    ) then\n'
        || E'        return ''purchase_by_identity'';\n'
        || E'    end if;\n';
    v_case constant text :=
        'public._portable_case_purchase_stop_reason(uuid)';
    v_case_anchor constant text :=
        E'\n    return null;\nend;\n';
    v_case_block constant text :=
        E'\n'
        || E'    -- portable_checkout_link_purchase_stop: la compra con nuestro link\n'
        || E'    -- (el sck de la emision) no escribe la correlacion portable, y el\n'
        || E'    -- caso del carrito pudo estar ya cerrado cuando compro.\n'
        || E'    if public._portable_checkout_link_purchase_exists(\n'
        || E'        v_binding.tenant_ref, v_binding.funnel_ref, v_case.contact_id, null,\n'
        || E'        (\n'
        || E'            select identity.external_user_id\n'
        || E'            from public.channel_identities identity\n'
        || E'            where identity.id = v_case.selected_channel_identity_id\n'
        || E'              and identity.contact_id = v_case.contact_id\n'
        || E'              and identity.channel = ''whatsapp''\n'
        || E'        )\n'
        || E'    ) then\n'
        || E'        return ''purchase_detected_any_offer'';\n'
        || E'    end if;\n'
        || v_case_anchor;
    v_signature text;
    v_anchor text;
    v_block text;
    v_definition text;
begin
    foreach v_signature in array array[v_precheckout, v_case] loop
        if v_signature = v_precheckout then
            v_anchor := v_precheckout_anchor;
            v_block := v_precheckout_block;
        else
            v_anchor := v_case_anchor;
            v_block := v_case_block;
        end if;
        if to_regprocedure(v_signature) is null then
            raise exception using errcode = '55000',
                message = 'portable_purchase_stop_function_missing',
                detail = v_signature;
        end if;
        v_definition := pg_get_functiondef(v_signature::regprocedure);
        if length(v_definition) - length(replace(v_definition, v_anchor, ''))
               <> length(v_anchor)
           or position('portable_checkout_link_purchase_stop' in v_definition) > 0 then
            raise exception using errcode = '55000',
                message = 'unexpected_portable_purchase_stop_definition',
                detail = v_signature;
        end if;
        execute replace(v_definition, v_anchor, v_block);
        v_definition := pg_get_functiondef(v_signature::regprocedure);
        if position(v_block in v_definition) = 0 then
            raise exception using errcode = '55000',
                message = 'portable_purchase_stop_not_replaced',
                detail = v_signature;
        end if;
    end loop;
end
$stops_see_our_link$;

commit;
