-- Migration: la compra de cualquier oferta del producto corta la secuencia de
-- recuperacion portable antes de cada toque, por el mail o por el telefono.
--
-- Por que. La compra corta un caso de carrito, pago fallido o ficha solo si es
-- de la misma oferta: apply_hotmart_purchase_approved (20260808000100:202)
-- busca el caso con rc.offer_code is not distinct from p_offer_code, y
-- reevaluate_followup_action (20260805000300) mira solo si el caso ya quedo
-- won. ATT1 vende el mismo producto (5071808) por varias ofertas: la de la
-- pauta, la de la landing B y alguna sin declarar. Quien abandono una y
-- compro por otra seguia en la secuencia. Con un toque no se notaba: el caso
-- termina en sequence_exhausted con el primero. Con A2 y Z, si.
--
-- El dato (att1-db, 19/10 ~03:00Z, solo lectura): de los casos portables, 5
-- tienen una compra del producto de la misma persona despues del caso, por
-- mail o telefono; en los 5 el toque salio antes de la compra. Uno compro por
-- otra oferta, a las 52 h del carrito: con Z le habria llegado el mensaje.
--
-- Que cambia:
-- 1. _portable_case_purchase_stop_reason(case): null si nada frena, o
--    'purchase_detected_any_offer'. Solo mira casos con procedencia portable
--    (commercial_ally_hotmart_event_bindings del evento que abrio el caso): los
--    casos de Johanna y del precheckout siguen como hoy (el precheckout tiene su
--    propio freno, _portable_precheckout_stop_reason, del que sale este). Frena
--    si la misma persona tiene, del mismo binding (tenant y funnel = producto):
--    - una compra admitida por la admision portable, de cualquier oferta y con
--      cualquier resultado de correlacion (resuelta, ambigua, en conflicto o
--      sin intencion), con el mail exacto o el telefono en cualquiera de sus
--      formas (_whatsapp_phone_variants) contra los contact_points del
--      contacto del caso o su identidad de WhatsApp;
--    - una intencion purchased del producto con el mismo mail o telefono;
--    - otro caso del producto ganado (purchase_event_id): la compra con
--      nuestro link (sck) entra por la correlacion de la emision y no escribe
--      la correlacion portable.
--    Sin ventana: a quien ya compro no se le escribe (D25 del precheckout).
--    Frena de mas antes que escribir de mas.
-- 2. reevaluate_followup_action suma una rama, despues de la compra del propio
--    caso: si el caso esta vivo y el helper devuelve un motivo, la accion se
--    cancela con ese motivo, la secuencia se completa y el caso queda
--    cancelled. No se gana: la venta no se atribuye a un toque que no la
--    produjo (D2 del precheckout). Se arma con reemplazos contados sobre la
--    definicion viva (20260805000300, su ultima definicion).
--
-- Que no cambia: la admision de la compra y el freno en el momento de la
-- compra (apply_hotmart_purchase_approved, los triggers de compra conocida),
-- que siguen ganando el caso de la misma oferta. El caso de otra oferta queda
-- abierto hasta su proxima accion, que la reevaluacion cancela antes de
-- mandar. plan_payment_failure_recovery no se toca (lo redefine
-- 20261019000200). El bridge no cambia: reevaluate_followup_action es la
-- reevaluacion del carrito, el pago fallido y la ficha.

begin;

create function public._portable_case_purchase_stop_reason(
    p_case_id uuid
)
returns text
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.recovery_cases%rowtype;
    v_binding public.commercial_ally_hotmart_event_bindings%rowtype;
begin
    -- Por que un toque de un caso portable no sale por una compra. Null si
    -- nada lo frena. De solo lectura. La llama reevaluate_followup_action, que
    -- es security invoker y corre como service_role, y service_role no lee las
    -- tablas de la compra portable: por eso es security definer y service_role
    -- la ejecuta.
    select rc.* into v_case
    from public.recovery_cases rc
    where rc.id = p_case_id;
    if not found then
        return null;
    end if;

    select binding.* into v_binding
    from public.commercial_ally_hotmart_event_bindings binding
    where binding.webhook_event_id = v_case.abandonment_event_id;
    if not found then
        return null;
    end if;

    -- Cualquier compra aprobada admitida del binding con la misma persona.
    if exists (
        select 1
        from public.portable_hotmart_purchase_correlations purchase
        join public.hotmart_purchase_intent_event_identities buyer
          on buyer.webhook_event_id = purchase.webhook_event_id
        where purchase.tenant_ref = v_binding.tenant_ref
          and purchase.funnel_ref = v_binding.funnel_ref
          and (
              exists (
                  select 1
                  from public.contact_points point
                  where point.contact_id = v_case.contact_id
                    and (
                        (
                            point.type = 'email'
                            and point.normalized_value = buyer.normalized_email
                        )
                        or (
                            point.type = 'phone'
                            and buyer.normalized_phone is not null
                            and point.normalized_value = any(
                                public._whatsapp_phone_variants(buyer.normalized_phone)
                            )
                        )
                    )
              )
              or exists (
                  select 1
                  from public.channel_identities identity
                  where identity.id = v_case.selected_channel_identity_id
                    and identity.contact_id = v_case.contact_id
                    and identity.channel = 'whatsapp'
                    and buyer.normalized_phone is not null
                    and identity.external_user_id = any(
                        public._whatsapp_phone_variants(buyer.normalized_phone)
                    )
              )
          )
    ) then
        return 'purchase_detected_any_offer';
    end if;

    -- Una intencion del producto que ya quedo purchased, de la misma persona.
    if exists (
        select 1
        from public.purchase_intents intent
        where intent.tenant_ref = v_binding.tenant_ref
          and intent.funnel_ref = v_binding.funnel_ref
          and intent.lifecycle_state = 'purchased'
          and exists (
              select 1
              from public.contact_points point
              where point.contact_id = v_case.contact_id
                and (
                    (
                        point.type = 'email'
                        and point.normalized_value = intent.normalized_email
                    )
                    or (
                        point.type = 'phone'
                        and point.normalized_value = any(
                            public._whatsapp_phone_variants(intent.normalized_phone)
                        )
                    )
                )
          )
    ) then
        return 'purchase_detected_any_offer';
    end if;

    -- Otro caso del mismo producto y la misma persona ya gano una compra (la
    -- del link con nuestro sck no escribe la correlacion portable).
    if exists (
        select 1
        from public.recovery_cases other
        where other.contact_id = v_case.contact_id
          and other.id <> v_case.id
          and other.external_product_id = v_case.external_product_id
          and other.purchase_event_id is not null
    ) then
        return 'purchase_detected_any_offer';
    end if;

    return null;
end;
$function$;

revoke all on function public._portable_case_purchase_stop_reason(uuid) from public;
do $grants$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public._portable_case_purchase_stop_reason(uuid) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public._portable_case_purchase_stop_reason(uuid) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public._portable_case_purchase_stop_reason(uuid) to service_role;
    end if;
end
$grants$;

-- La rama de la reevaluacion, sobre la definicion viva, con el texto contado.
do $reevaluate_purchase_any_offer$
declare
    v_signature constant text :=
        'public.reevaluate_followup_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)';
    v_old_branch constant text :=
        E'    elsif v_case.purchase_event_id is not null or v_case.status = ''won'' then\n'
        || E'        v_decision := ''cancel'';\n'
        || E'        v_reason := ''purchase_detected'';\n';
    v_new_branch constant text :=
        v_old_branch
        || E'    elsif v_case.status in (''grace_period'', ''active'')\n'
        || E'       and public._portable_case_purchase_stop_reason(v_case.id) is not null then\n'
        || E'        -- portable_purchase_stops_any_offer: la misma persona compro el\n'
        || E'        -- producto por cualquier oferta. El caso se cancela, no se gana.\n'
        || E'        v_decision := ''cancel'';\n'
        || E'        v_reason := public._portable_case_purchase_stop_reason(v_case.id);\n';
    v_old_close constant text :=
        E'        elsif v_reason in (\n'
        || E'            ''purchase_detected'', ''contact_blocked'', ''contact_authorization_denied''\n'
        || E'        ) then\n';
    v_new_close constant text :=
        E'        elsif v_reason in (\n'
        || E'            ''purchase_detected'', ''contact_blocked'', ''contact_authorization_denied'',\n'
        || E'            ''purchase_detected_any_offer''\n'
        || E'        ) then\n';
    v_definition text;
begin
    if to_regprocedure(v_signature) is null then
        raise exception using errcode = '55000',
            message = 'portable_purchase_stop_function_missing',
            detail = v_signature;
    end if;
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, v_old_branch, ''))
           <> length(v_old_branch)
       or length(v_definition) - length(replace(v_definition, v_old_close, ''))
           <> length(v_old_close)
       or position('portable_purchase_stops_any_offer' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_followup_reevaluation_definition',
            detail = v_signature;
    end if;
    v_definition := replace(v_definition, v_old_branch, v_new_branch);
    v_definition := replace(v_definition, v_old_close, v_new_close);
    execute v_definition;
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if position(v_new_branch in v_definition) = 0
       or position(v_new_close in v_definition) = 0 then
        raise exception using errcode = '55000',
            message = 'followup_reevaluation_not_replaced',
            detail = v_signature;
    end if;
end
$reevaluate_purchase_any_offer$;

commit;
