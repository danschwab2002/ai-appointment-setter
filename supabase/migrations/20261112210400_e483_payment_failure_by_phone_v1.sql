-- e483-payment-failure-by-phone-v1: el pago rechazado de una oferta de campana
-- abre el recorrido nativo aunque la persona no haya llenado el formulario,
-- con el telefono del evento de Hotmart (E-483, pieza PF, 2026-11-12).
-- Contrato: docs/contracts/flow-engine-v2.md («El pago rechazado sin formulario
-- de una oferta de campana»).
--
-- Por que. Medido en ATT1 el 12/11: la oferta Black wmkq4ba9 tiene una campana
-- del Automation Expert con codigo_oferta (E-410/E-411). Sus pagos rechazados
-- quedan en payment_failure_correlation_unresolved: la persona no lleno el
-- formulario y el permiso del pago rechazado (20261030000200) exige la
-- intencion que la admision portable correlaciono con el rechazo. Resultado: ni
-- caso ni mensaje. La etiqueta de la campana (campaign-entry-v1 §4b) la pone el
-- bridge en el before_send de un recorrido nativo que YA existe
-- (flow_engine_native_campaign_origin_v1 lee la oferta del recorrido aunque no
-- tenga intencion); lo que faltaba es abrir el recorrido.
--
-- Que cambia:
--   1. flow_engine_native_runs: la columna audience_hotmart_phone (falso por
--      defecto) y la restriccion audience_by_trigger: un pago rechazado puede
--      nacer sin intencion solo con audience_hotmart_phone.
--   2. _flow_engine_native_payment_failure_phone_audience(evento, contacto,
--      oferta, destino, tenant), privada: el permiso del telefono de Hotmart.
--      Entra (null) solo si el evento es un PURCHASE_CANCELED de Hotmart de la
--      instancia y de esa oferta (la procedencia del binding portable), la
--      admision portable NO encontro ninguna intencion (correlation_outcome
--      unmatched; resuelto lo decide el formulario, y ambiguo o en conflicto
--      no entra), el evento trae su telefono (checkout_phone, o phone) y es el
--      destino, y el contacto
--      tiene ese movil de Hotmart o del sistema. Un cancelado que no es un
--      rechazo da no_es_un_rechazo; todo lo demas, no_entra:permiso.
--   3. admit_flow_engine_event_v1: con el pago rechazado, si el bridge manda
--      p_instance.native_payment_failure_by_phone_offers (la lista de ofertas de
--      campana; FLOW_ENGINE_NATIVE_PAYMENT_FAILURE_BY_PHONE_ENABLED), la deja en
--      la variable de la transaccion e483.payment_failure_by_phone_offers para
--      la admision de cada flujo, y la borra al terminar.
--   4. _flow_engine_admit_native_flow: con el pago rechazado, si el permiso
--      formulario dio no_entra:permiso y la oferta del evento esta en esa lista,
--      prueba el permiso de 2. Si entra: el recorrido nace sin intencion y con
--      audience_hotmart_phone, y la autorizacion queda con la evidencia
--      hotmart_payment_failure_phone (fuente hotmart). La baja, la repeticion,
--      la cohorte, el doble caso y la bolsa, los de siempre.
--   5. _flow_engine_authorize_native_tail: un recorrido con
--      audience_hotmart_phone no tiene intencion que mirar otra vez; la puerta
--      mira la baja (la del contacto y la de Chatwoot de ese movil) antes de
--      cada envio.
--
-- Lo que NO cambia. Sin la clave en p_instance (el bridge de hoy, o con el flag
-- apagado) la admision es exactamente la de antes: el rechazo sin formulario
-- sigue en no_entra:permiso (y el legado, en payment_failure_correlation_unresolved).
-- Una oferta que no esta en la lista, igual. El legado (ATT1 con B1/B2,
-- Maribel con su scope propio) no se toca. Las filas existentes quedan con
-- audience_hotmart_phone falso y cumplen la restriccion nueva.
--
-- Como: el metodo de 20261112210000, reemplazo contado sobre la definicion
-- VIGENTE, con la marca que no puede estar. Correrla dos veces aborta con 55000.
--
-- Errores: 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $guard$
begin
    if to_regprocedure('public._flow_engine_native_payment_failure_phone_audience(uuid,uuid,text,text,text)') is not null then
        raise exception using errcode = '55000', message = 'e483_payment_failure_by_phone_already_applied';
    end if;
    if to_regprocedure('public._flow_engine_native_payment_failure_audience(uuid,uuid,text,text,text)') is null
       or to_regprocedure('public.admit_flow_engine_event_v1(jsonb,jsonb)') is null
       or position('e483_native_priority_wins: end' in pg_get_functiondef(
              'public.admit_flow_engine_event_v1(jsonb,jsonb)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_payment_failure_by_phone_missing';
    end if;
end;
$guard$;

-- Los reemplazos por ancla, con la misma verificacion para todos.
create function pg_temp.e483_payment_failure_by_phone_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
    v_acl text;
    v_secdef boolean;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'e483_payment_failure_by_phone_function_missing',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_e483_payment_failure_by_phone_definition',
            detail = p_signature;
    end if;
    select coalesce(p.proacl::text, ''), p.prosecdef into v_acl, v_secdef
      from pg_proc p where p.oid = p_signature::regprocedure;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_payment_failure_by_phone_not_replaced',
            detail = p_signature;
    end if;
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = p_signature::regprocedure) is distinct from v_acl
       or (select p.prosecdef from pg_proc p where p.oid = p_signature::regprocedure) is distinct from v_secdef then
        raise exception using errcode = '55000', message = 'e483_payment_failure_by_phone_acl_changed',
            detail = p_signature;
    end if;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 1. La fila del recorrido.
-- ---------------------------------------------------------------------------
alter table public.flow_engine_native_runs
    add column audience_hotmart_phone boolean not null default false,
    drop constraint flow_engine_native_runs_audience_by_trigger,
    add constraint flow_engine_native_runs_audience_by_trigger check (
        -- e483_payment_failure_by_phone: el pago rechazado de una oferta de
        -- campana, con el telefono de Hotmart y sin intencion (20261112210400).
        (audience_hotmart_phone
         and trigger_event = 'pago_fallido'
         and audience_purchase_intent_id is null and audience_precheckout_submission_id is null)
        or (not audience_hotmart_phone
            and trigger_event in ('carrito', 'pago_fallido', 'intencion')
            and audience_purchase_intent_id is not null and audience_precheckout_submission_id is not null)
        or (not audience_hotmart_phone
            and trigger_event not in ('carrito', 'pago_fallido', 'intencion')
            and audience_purchase_intent_id is null and audience_precheckout_submission_id is null));

-- ---------------------------------------------------------------------------
-- 2. El permiso del telefono de Hotmart.
-- ---------------------------------------------------------------------------
create function public._flow_engine_native_payment_failure_phone_audience(
    p_webhook_event_id uuid, p_contact_id uuid, p_offer text, p_destination text, p_tenant_ref text)
returns text
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- e483_payment_failure_by_phone: el permiso sin formulario (20261112210400).
    v_event public.webhook_events%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_details public.commercial_ally_payment_failure_details%rowtype;
    v_phone text;
begin
    select event_row.* into v_event
      from public.webhook_events event_row
     where event_row.id = p_webhook_event_id and event_row.source = 'hotmart'
       and event_row.event_type = 'PURCHASE_CANCELED';
    if not found then
        return 'no_entra:permiso';
    end if;
    -- La procedencia del binding portable: de la instancia y de esa oferta.
    select provenance.* into v_provenance
      from public.commercial_ally_hotmart_event_bindings provenance
     where provenance.webhook_event_id = p_webhook_event_id;
    if not found or v_provenance.tenant_ref is distinct from p_tenant_ref or p_offer is null
       or v_provenance.offer_ref is distinct from p_offer then
        return 'no_entra:permiso';
    end if;
    -- Solo sin ninguna intencion: resuelto es del formulario (y si el formulario
    -- no dio permiso, no se pisa con el telefono); ambiguo o en conflicto, no.
    select details.* into v_details
      from public.commercial_ally_payment_failure_details details
     where details.webhook_event_id = p_webhook_event_id
       and details.trigger_kind = 'payment_failure';
    if not found or v_details.correlation_outcome is distinct from 'unmatched'
       or v_details.purchase_intent_id is not null then
        return 'no_entra:permiso';
    end if;
    if public.payment_failure_refusal_kind(v_details.refusal_reason,
           v_event.payload #>> '{data,purchase,payment,type}') = 'not_a_refusal' then
        return 'no_es_un_rechazo';
    end if;
    -- El telefono es el del evento (checkout_phone, y si no viene phone, como
    -- parse_hotmart_payment_failure_payload del bridge): sin telefono, o con otro
    -- destino (la identidad que guardo un formulario de otro momento), no entra.
    v_phone := coalesce(nullif(btrim(v_event.payload #>> '{data,buyer,checkout_phone}'), ''),
                        nullif(btrim(v_event.payload #>> '{data,buyer,phone}'), ''));
    if v_phone is null or p_destination is null
       or public._whatsapp_phone_canonical(v_phone) is distinct from public._whatsapp_phone_canonical(p_destination) then
        return 'no_entra:permiso';
    end if;
    -- El contacto tiene ese movil (payment_failure_contact_mismatch del legado).
    if not exists (
        select 1 from public.contact_points point
         where point.contact_id = p_contact_id and point.type = 'phone'
           and point.normalized_value = any(public._whatsapp_phone_variants(p_destination))
           and point.source in ('hotmart', 'system')) then
        return 'no_entra:permiso';
    end if;
    return null;
end;
$function$;

do $acl$
declare
    v_role text;
begin
    revoke all on function public._flow_engine_native_payment_failure_phone_audience(uuid,uuid,text,text,text) from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function public._flow_engine_native_payment_failure_phone_audience(uuid,uuid,text,text,text) from %I',
            v_role);
    end loop;
end;
$acl$;

-- ---------------------------------------------------------------------------
-- 3 a 5. Los reemplazos sobre la definicion vigente.
-- ---------------------------------------------------------------------------
do $patch$
declare
    c_event constant text := 'public.admit_flow_engine_event_v1(jsonb,jsonb)';
    c_admit constant text :=
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)';
    c_tail constant text :=
        'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)';
begin
    -- 3. La lista de ofertas de campana, para la admision de cada flujo.
    perform pg_temp.e483_payment_failure_by_phone_patch(c_event,
        E'    -- 4.2 Cada flujo en su subtransaccion: la falla de uno no frena a los otros.\n'
        || E'    foreach v_flow_id in array v_flow_ids loop\n',
        E'    -- e483_payment_failure_by_phone_offers: begin (20261112210400)\n'
        || E'    -- El pago rechazado sin formulario de una oferta de campana, solo si el bridge\n'
        || E'    -- manda la lista (FLOW_ENGINE_NATIVE_PAYMENT_FAILURE_BY_PHONE_ENABLED). Vive en la\n'
        || E'    -- transaccion y se borra al terminar el bucle.\n'
        || E'    perform set_config(''e483.payment_failure_by_phone_offers'',\n'
        || E'        case when v_type = ''pago_fallido''\n'
        || E'                  and jsonb_typeof(p_instance -> ''native_payment_failure_by_phone_offers'') = ''array''\n'
        || E'             then (p_instance -> ''native_payment_failure_by_phone_offers'')::text\n'
        || E'             else '''' end,\n'
        || E'        true);\n'
        || E'    -- e483_payment_failure_by_phone_offers: end\n'
        || E'    -- 4.2 Cada flujo en su subtransaccion: la falla de uno no frena a los otros.\n'
        || E'    foreach v_flow_id in array v_flow_ids loop\n',
        'e483_payment_failure_by_phone_offers: begin');
    perform pg_temp.e483_payment_failure_by_phone_patch(c_event,
        E'            v_one.followup_sequence_id, v_one.scheduled_action_id, v_one.created;\n'
        || E'    end loop;\n',
        E'            v_one.followup_sequence_id, v_one.scheduled_action_id, v_one.created;\n'
        || E'    end loop;\n'
        || E'    -- e483_payment_failure_by_phone_offers_reset\n'
        || E'    perform set_config(''e483.payment_failure_by_phone_offers'', '''', true);\n',
        'e483_payment_failure_by_phone_offers_reset');

    -- 4. La admision: las variables.
    perform pg_temp.e483_payment_failure_by_phone_patch(c_admit,
        E'    v_audience_submission uuid;\n'
        || E'begin\n',
        E'    v_audience_submission uuid;\n'
        || E'    -- e483_payment_failure_by_phone_vars: el permiso del telefono de Hotmart (20261112210400).\n'
        || E'    v_audience_by_phone boolean := false;\n'
        || E'begin\n',
        'e483_payment_failure_by_phone_vars');
    -- El permiso: despues del formulario, solo si el formulario no dio permiso.
    perform pg_temp.e483_payment_failure_by_phone_patch(c_admit,
        E'              p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref) audience;\n'
        || E'    -- flow_engine_native_intent_permission:',
        E'              p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref) audience;\n'
        || E'        -- e483_payment_failure_by_phone_permission: begin (20261112210400)\n'
        || E'        -- Sin formulario, la oferta de una campana entra con el telefono de Hotmart,\n'
        || E'        -- solo si el bridge mando la lista y la oferta esta en ella.\n'
        || E'        if v_reason = ''no_entra:permiso''\n'
        || E'           and coalesce(nullif(current_setting(''e483.payment_failure_by_phone_offers'', true), '''')::jsonb\n'
        || E'                        ? p_offer, false) then\n'
        || E'            v_reason := public._flow_engine_native_payment_failure_phone_audience(\n'
        || E'                p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref);\n'
        || E'            v_audience_by_phone := v_reason is null;\n'
        || E'        end if;\n'
        || E'        -- e483_payment_failure_by_phone_permission: end\n'
        || E'    -- flow_engine_native_intent_permission:',
        'e483_payment_failure_by_phone_permission: begin');
    -- La autorizacion: la evidencia es el rechazo de Hotmart, no una compra.
    perform pg_temp.e483_payment_failure_by_phone_patch(c_admit,
        E'            case when v_audience_intent is null\n'
        || E'                 then jsonb_build_object(''reason'', ''purchase_approved'',',
        E'            case when v_audience_by_phone\n'
        || E'                 -- e483_payment_failure_by_phone_grant (20261112210400)\n'
        || E'                 then jsonb_build_object(''reason'', ''hotmart_payment_failure_phone'',\n'
        || E'                                         ''webhook_event_id'', p_event.id, ''offer'', p_offer,\n'
        || E'                                         ''recovery_case_id'', v_case_id)\n'
        || E'                 when v_audience_intent is null\n'
        || E'                 then jsonb_build_object(''reason'', ''purchase_approved'',',
        'e483_payment_failure_by_phone_grant');
    -- El recorrido: la marca del permiso.
    perform pg_temp.e483_payment_failure_by_phone_patch(c_admit,
        E'        audience_precheckout_submission_id)\n'
        || E'    values (v_case_id, public._flow_engine_native_trigger(v_definition.definition), p_offer,\n'
        || E'        p_account_id, p_inbox_id, v_audience_intent, v_audience_submission);\n',
        E'        audience_precheckout_submission_id,\n'
        || E'        -- e483_payment_failure_by_phone_run (20261112210400)\n'
        || E'        audience_hotmart_phone)\n'
        || E'    values (v_case_id, public._flow_engine_native_trigger(v_definition.definition), p_offer,\n'
        || E'        p_account_id, p_inbox_id, v_audience_intent, v_audience_submission, v_audience_by_phone);\n',
        'e483_payment_failure_by_phone_run');

    -- 5. La puerta antes de cada envio.
    perform pg_temp.e483_payment_failure_by_phone_patch(c_tail,
        E'    if v_native.trigger_event in (''carrito'', ''pago_fallido'') then\n',
        E'    -- e483_payment_failure_by_phone_recheck: begin (20261112210400)\n'
        || E'    -- El pago rechazado que entro con el telefono de Hotmart no tiene intencion:\n'
        || E'    -- la puerta mira la baja del contacto y la de Chatwoot de ese movil.\n'
        || E'    if v_native.audience_hotmart_phone then\n'
        || E'        if exists (\n'
        || E'               select 1 from public.contacts contact_row\n'
        || E'                where contact_row.id = p_case.contact_id\n'
        || E'                  and (contact_row.contact_permission in (''opted_out'', ''blocked'', ''restricted'')\n'
        || E'                       or contact_row.lifecycle_status = ''do_not_contact''))\n'
        || E'           or public._portable_chatwoot_opt_out_stop(v_native.chatwoot_account_id, p_case.contact_id,\n'
        || E'                  v_identity.external_user_id) then\n'
        || E'            return query select ''rechazado''::text, ''baja''::text, null::uuid, p_control.generation, false;\n'
        || E'            return;\n'
        || E'        end if;\n'
        || E'    end if;\n'
        || E'    -- e483_payment_failure_by_phone_recheck: end\n'
        || E'    if v_native.trigger_event in (''carrito'', ''pago_fallido'') and not v_native.audience_hotmart_phone then\n',
        'e483_payment_failure_by_phone_recheck: begin');
end;
$patch$;

commit;
