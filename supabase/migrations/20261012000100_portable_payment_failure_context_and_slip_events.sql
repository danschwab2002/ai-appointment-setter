-- Migration: el ultimo pago rechazado para la entrada del agente, y los eventos
-- de la ficha de pago (OXXO y parecidos) guardados, en el runtime con
-- manifiesto. Version 1.6.0.
--
-- Por que. Decision del 2026-10-12 «como se resuelve cada pago que no se
-- completa»: el agente recibe el motivo, el medio, las cuotas y el pais del
-- pago rechazado, y el bridge guarda los eventos de la ficha para el
-- seguimiento de la ficha sin pagar (Etapa 1). Hasta la 1.5.0 el agente no
-- sabia nada del pago (la ruta «Compra fallida» del SOUL derivaba), y el
-- bridge contestaba 200 a PURCHASE_BILLET_PRINTED y PURCHASE_EXPIRED sin
-- guardarlos (unsupported_event_type).
--
-- Que crea. Dos funciones nuevas, ninguna tabla:
--
-- 1. get_portable_recent_payment_failure_v1(tenant, funnel, telefono): el
--    PURCHASE_CANCELED mas reciente de la ultima semana de ese movil (por sus
--    dos formas, 52/521 y 54/549, con _whatsapp_phone_variants de
--    20261001000100), de la aliada del binding (por
--    commercial_ally_hotmart_event_bindings, que escribe la admision portable
--    del pago fallido), y solo si despues no hubo un PURCHASE_APPROVED del
--    mismo producto para ese movil. Devuelve una fila o ninguna, solo con lo
--    que el agente usa: la hora, la oferta, el medio, las cuotas, el motivo y
--    el pais. Nada de email, nombre, documento ni transaccion. Solo lee.
-- 2. admit_portable_hotmart_payment_slip_event_v1(tenant, funnel, version,
--    id, payload): guarda un PURCHASE_BILLET_PRINTED o PURCHASE_EXPIRED en
--    webhook_events con su procedencia, igual que la admision portable del
--    pago fallido pero sin correlacionar ni planificar nada: el consumidor es
--    el seguimiento de la Etapa 1. Un evento de otro producto u otra oferta
--    no se guarda y vuelve como ignored_binding_mismatch, sin error: no es una
--    falla del webhook, y Hotmart desactiva el webhook entero si se le acumulan
--    rechazos.
--
-- La forma del payload. La de PURCHASE_CANCELED esta medida en ATT1 (cuatro
-- eventos del 2026-10-11, fixture hotmart_purchase_canceled_att1_20261012.json):
-- data.purchase.payment trae type, installments_number y refusal_reason, y
-- data.purchase.checkout_country trae iso. La de los eventos de la ficha
-- todavia no: ATT1 no los tenia suscriptos. Por eso la admision solo depende
-- del sobre que comparten todos los eventos de compra 2.0.0 (id, event,
-- version, data.product.id, data.purchase.offer.code) y guarda el payload
-- entero; lo propio de la ficha se lee cuando llegue el primero real.

begin;

create or replace function public.get_portable_recent_payment_failure_v1(
    p_tenant_ref text,
    p_funnel_ref text,
    p_whatsapp_phone text
) returns table (
    failed_at timestamptz,
    offer_code text,
    payment_type text,
    installments_number integer,
    refusal_reason text,
    checkout_country_iso text
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
       or nullif(btrim(p_funnel_ref), '') is null
       or p_whatsapp_phone is null
       or p_whatsapp_phone !~ '^[1-9][0-9]{7,14}$' then
        raise exception using errcode = '22023',
            message = 'invalid_portable_recent_payment_failure_input';
    end if;
    v_variants := public._whatsapp_phone_variants(p_whatsapp_phone);

    -- recent_payment_failure: begin
    return query
    with failures as (
        select
            event.received_at,
            event.payload,
            nullif(regexp_replace(coalesce(
                event.payload #>> '{data,buyer,checkout_phone}',
                event.payload #>> '{data,buyer,phone}',
                ''
            ), '[^0-9]', '', 'g'), '') as phone
        from public.webhook_events event
        join public.commercial_ally_hotmart_event_bindings provenance
          on provenance.webhook_event_id = event.id
        where event.source = 'hotmart'
          and event.event_type = 'PURCHASE_CANCELED'
          and event.received_at >= clock_timestamp() - interval '7 days'
          and provenance.tenant_ref = p_tenant_ref
          and provenance.funnel_ref = p_funnel_ref
    )
    select
        failure.received_at,
        failure.payload #>> '{data,purchase,offer,code}',
        nullif(btrim(failure.payload #>> '{data,purchase,payment,type}'), ''),
        case
            when failure.payload #>> '{data,purchase,payment,installments_number}' ~ '^[0-9]{1,3}$'
                then (failure.payload #>> '{data,purchase,payment,installments_number}')::integer
        end,
        nullif(btrim(failure.payload #>> '{data,purchase,payment,refusal_reason}'), ''),
        upper(nullif(btrim(coalesce(
            failure.payload #>> '{data,purchase,checkout_country,iso}',
            failure.payload #>> '{data,buyer,address,country_iso}'
        )), ''))
    from failures failure
    where failure.phone = any(v_variants)
      -- Quien compro despues del rechazo ya no tiene un pago rechazado.
      and not exists (
          select 1
          from public.webhook_events approved
          where approved.source = 'hotmart'
            and approved.event_type = 'PURCHASE_APPROVED'
            and approved.received_at > failure.received_at
            and approved.payload #>> '{data,product,id}'
                = failure.payload #>> '{data,product,id}'
            and nullif(regexp_replace(coalesce(
                approved.payload #>> '{data,buyer,checkout_phone}',
                approved.payload #>> '{data,buyer,phone}',
                ''
            ), '[^0-9]', '', 'g'), '') = any(v_variants)
      )
    order by failure.received_at desc
    limit 1;
    -- recent_payment_failure: end
end;
$function$;

create or replace function public.admit_portable_hotmart_payment_slip_event_v1(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb
) returns table (outcome text, webhook_event_id uuid)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_existing public.webhook_events%rowtype;
    v_event_type text;
    v_offer_code text;
    v_event_id uuid;
begin
    v_event_type := p_payload ->> 'event';
    v_offer_code := p_payload #>> '{data,purchase,offer,code}';
    if nullif(btrim(p_external_event_id), '') is null
       or p_payload is null
       or jsonb_typeof(p_payload) <> 'object'
       or p_payload ->> 'id' is distinct from p_external_event_id
       or p_payload ->> 'version' is distinct from '2.0.0'
       or v_event_type is null
       or v_event_type not in ('PURCHASE_BILLET_PRINTED', 'PURCHASE_EXPIRED')
       or p_binding_version is null
       or p_binding_version < 1 then
        raise exception using errcode = '22023',
            message = 'invalid_portable_hotmart_payment_slip_input';
    end if;

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = p_tenant_ref
      and binding.funnel_ref = p_funnel_ref
      and binding.binding_version = p_binding_version
      and binding.status = 'active';
    if not found then
        raise exception using errcode = '22023',
            message = 'commercial_ally_binding_unavailable';
    end if;

    -- payment_slip_binding: begin
    if coalesce(p_payload #>> '{data,product,id}', '') !~ '^[0-9]{1,18}$'
       or (p_payload #>> '{data,product,id}')::numeric
          is distinct from v_binding.hotmart_product_id::numeric
       or not coalesce(
              v_offer_code = v_binding.offer_code
              or v_offer_code = any(v_binding.additional_offer_codes),
              false
          ) then
        return query select 'ignored_binding_mismatch'::text, null::uuid;
        return;
    end if;
    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.tenant_ref = v_binding.tenant_ref
      and scope.funnel_ref = v_binding.funnel_ref
      and scope.hotmart_product_id = v_binding.hotmart_product_id::text
      and scope.purchase_intent_product_ref = v_binding.product_hotlink
      and scope.offer_ref = v_offer_code
      and scope.active;
    if not found then
        return query select 'ignored_binding_mismatch'::text, null::uuid;
        return;
    end if;
    -- payment_slip_binding: end

    perform pg_advisory_xact_lock(
        hashtextextended('payment-slip:' || p_external_event_id, 0)
    );
    select event.* into v_existing
    from public.webhook_events event
    where event.source = 'hotmart'
      and event.external_event_id = p_external_event_id;
    if found then
        if v_existing.event_type = v_event_type and v_existing.payload = p_payload then
            return query select 'duplicate'::text, v_existing.id;
        else
            return query select 'semantic_conflict'::text, v_existing.id;
        end if;
        return;
    end if;

    insert into public.webhook_events (
        source, external_event_id, event_type, payload, processing_status
    ) values (
        'hotmart', p_external_event_id, v_event_type, p_payload, 'received'
    ) returning id into v_event_id;
    insert into public.commercial_ally_hotmart_event_bindings (
        webhook_event_id, scope_id, tenant_ref, funnel_ref, binding_version,
        hotmart_product_id, purchase_intent_product_ref, offer_ref
    ) values (
        v_event_id, v_scope.id, v_binding.tenant_ref, v_binding.funnel_ref,
        v_binding.binding_version, v_scope.hotmart_product_id,
        v_scope.purchase_intent_product_ref, v_scope.offer_ref
    );
    return query select 'inserted'::text, v_event_id;
end;
$function$;

-- Supabase le da execute por defecto a toda funcion nueva: se revoca a todos y
-- quedan solo para service_role, el rol del bridge.
do $roles$
declare
    v_signature text;
begin
    foreach v_signature in array array[
        'public.get_portable_recent_payment_failure_v1(text,text,text)',
        'public.admit_portable_hotmart_payment_slip_event_v1(text,text,integer,text,jsonb)'
    ] loop
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
                message = 'portable_payment_failure_context_acl_unexpected',
                detail = format('solo service_role ejecuta %s', v_signature);
        end if;
    end loop;
end
$roles$;

commit;
