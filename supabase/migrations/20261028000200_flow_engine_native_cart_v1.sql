-- flow-engine-native-cart-v1: el motor nativo arranca tambien con el carrito
-- abandonado (E-161, el PR 2 del hito 4 de la version 1).
--
-- Por que. El Automation Expert solo crea flujos nativos, y el nativo solo
-- sabia la compra: el carrito de Maribel (un toque, la plantilla MARKETING
-- maribel_retomar_proceso_01) no cabia. La forma del evento ya aceptaba
-- carrito (admit_flow_engine_event_v1, 20261024000200), pero el disparador
-- (_flow_engine_native_trigger), lo que el motor ejecuta
-- (_flow_engine_native_unsupported), la forma de pasos y la fila del
-- recorrido (flow_engine_native_runs) solo admitian compra. Contrato:
-- docs/contracts/flow-engine-v2.md, seccion 19 (2.7.0).
--
-- Que cambia, por bloques:
--
-- 1. flow_engine_native_runs: trigger_event admite carrito, y dos columnas
--    nuevas, la intencion con consentimiento y el envio del formulario que dan
--    el permiso de un carrito (las dos o ninguna, y solo con el carrito).
-- 2. Lo que el motor nativo ejecuta, reemplazado entero (son de una linea):
--    _flow_engine_native_trigger (compra o carrito), _flow_engine_native_steps
--    (nativo:carrito, un envio con las variables de nombre y producto, la misma
--    forma que nativo:compra) y _flow_engine_native_unsupported (con la compra,
--    permiso compra; con el carrito, permiso formulario; y desde ahora un solo
--    envio, motor_nativo:forma, porque una definicion del carrito del legado ya
--    pasa el disparador y el permiso; y etiquetar solo con la compra).
-- 3. El permiso del carrito: _flow_engine_native_cart_audience (la intencion
--    que la admision portable correlaciono con ESTE carrito, de la instancia y
--    de la oferta del evento, con el criterio de consentimiento de hoy,
--    _portable_consented_intent_reason) y _flow_engine_native_cart_reason (el
--    mismo criterio sobre una intencion ya guardada, para el arranque).
--    Los motivos son los del carrito del motor (20261022000100): baja,
--    ya_no_entra o no_entra:permiso.
-- 4. Reemplazos contados sobre la definicion VIGENTE:
--    _flow_engine_admit_native_flow (20261024000200 + 20261026000200): el
--      permiso del carrito, la autorizacion que concede con la intencion (como
--      el pago rechazado portable) y la fila del recorrido con la intencion;
--    _flow_engine_authorize_native_tail (20261024000200): la puerta vuelve a
--      mirar la intencion antes del envio, con la intencion FOR SHARE, como el
--      carrito del motor;
--    read_flow_engine_native_trigger_counts_v1 (20261024000600): la
--      simulacion de un item nuevo cuenta tambien los carritos;
--    read_automation_expert_native_history_v1 (20261025000100): el momento en
--      que arranco un recorrido del carrito dice PURCHASE_OUT_OF_SHOPPING_CART.
-- 5. flow_engine_execution_context_v1 se vuelve a crear con una columna mas,
--    entry_permission (el entra.permiso de la version fijada), para que el
--    envio de un paso nativo sepa que categoria acepta (2.6.0: con compra solo
--    UTILITY; con formulario tambien MARKETING). Cambia el tipo de salida, asi
--    que va drop y create, con los mismos permisos: solo service_role. Un
--    bridge de antes lee la fila por nombre y no mira la columna nueva.
--
-- Lo que no cambia. Sin un flujo nativo del carrito, nada de esto corre: la
-- admision del carrito (portable y del motor v1) no se toca, las lecturas de
-- la v1 siguen sin lo nativo, y la compra se ejecuta igual que antes (lo mide
-- validate_flow_engine_native_cart.mjs con la bienvenida de ATT1). La
-- compra corta un carrito nativo antes del envio por el freno de hoy
-- (_portable_case_purchase_stop_reason, 20261019000300): el caso nace del
-- evento del carrito, que tiene su binding portable. El caso sigue con
-- offer_code nulo (los planificadores no lo ven) y la oferta en el context.
--
-- Lo que no hace: no crea roles, no cambia el ACL del esquema, no toca filas
-- que existan y no agrega claves foraneas a tablas del sustrato. Correrla dos
-- veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.flow_engine_native_runs') is null
       or to_regprocedure('public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)') is null
       or to_regprocedure('public.flow_engine_execution_context_v1(uuid)') is null
       or to_regprocedure('public._portable_consented_intent_reason(uuid,uuid,text)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_missing';
    end if;
    if to_regprocedure('public._flow_engine_native_cart_audience(uuid,uuid,text,text,text)') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_native_cart_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla, con la misma verificacion para todos.
create function pg_temp.flow_engine_native_cart_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_cart_function_missing', detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_cart_owner_mismatch', detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_cart_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_cart_definition_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- Bloque 1. La fila del recorrido.
-- ===========================================================================

alter table public.flow_engine_native_runs
    drop constraint flow_engine_native_runs_trigger_event_check,
    add constraint flow_engine_native_runs_trigger_event_check check (trigger_event in ('compra', 'carrito')),
    add column audience_purchase_intent_id uuid,
    add column audience_precheckout_submission_id uuid,
    add constraint flow_engine_native_runs_audience_by_trigger check (
        (trigger_event = 'carrito'
         and audience_purchase_intent_id is not null and audience_precheckout_submission_id is not null)
        or (trigger_event <> 'carrito'
            and audience_purchase_intent_id is null and audience_precheckout_submission_id is null));

-- ===========================================================================
-- Bloque 2. Lo que el motor nativo ejecuta.
-- ===========================================================================

-- El disparador de un flujo nativo que ejecuta esta version, o null.
create or replace function public._flow_engine_native_trigger(p_definition jsonb)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    -- flow_engine_native_cart: compra, y desde 20261028000200 el carrito.
    select case
        when p_definition -> 'dispara' = '{"evento": "compra"}'::jsonb then 'compra'
        when p_definition -> 'dispara' = '{"evento": "carrito"}'::jsonb then 'carrito'
    end
$function$;

-- La forma de los pasos de un flujo nativo: sale del disparador. Los dos son
-- un solo envio, con las variables que el bridge sabe llenar (variables_de,
-- 20261026000100). Null para todo lo demas: el legado sigue con
-- _flow_engine_legacy_steps.
create or replace function public._flow_engine_native_steps(p_kind text)
returns jsonb
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select case p_kind
        when 'nativo:compra' then
            '[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "variables_de": ["nombre", "producto"], "coupon_variables": null}]'::jsonb
        -- flow_engine_native_cart: el carrito de un toque (20261028000200).
        when 'nativo:carrito' then
            '[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "variables_de": ["nombre", "producto"], "coupon_variables": null}]'::jsonb
    end
$function$;

-- Por que el motor nativo de esta version no ejecuta una definicion, o null.
-- La ejecuta si dispara con la compra y entra con el permiso compra, o con el
-- carrito y el permiso formulario, y repite por persona o por persona y oferta.
create or replace function public._flow_engine_native_unsupported(p_definition jsonb)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when public._flow_engine_native_trigger(p_definition) is null then 'motor_nativo:disparador'
        -- flow_engine_native_cart: el permiso sale del disparador (20261028000200).
        when p_definition #> '{entra,permiso}' is distinct from
             (case public._flow_engine_native_trigger(p_definition)
                  when 'compra' then '"compra"' else '"formulario"' end)::jsonb then 'motor_nativo:permiso'
        when coalesce(p_definition #>> '{topes,repeticion,una_por}', '') not in ('persona', 'persona_y_oferta')
            then 'motor_nativo:repeticion'
        when p_definition #> '{topes,repeticion,otra_despues_de}' is distinct from '"nunca"'::jsonb
             and public._flow_engine_duration_minutes(p_definition #> '{topes,repeticion,otra_despues_de}') is null
            then 'motor_nativo:repeticion'
        when p_definition -> 'prioridad' ? 'no_arranca_mientras'
             and jsonb_typeof(p_definition #> '{prioridad,no_arranca_mientras}') is distinct from 'array'
            then 'motor_nativo:prioridad'
        -- flow_engine_native_cart_shape: un solo envio, y solo esperar, plantilla y
        -- etiquetar (20261028000200). Con el carrito, una definicion del legado (A1,
        -- A2 y Z) ya pasa el disparador y el permiso: la base no la abre aunque el
        -- validador la dejara pasar.
        when (select count(*) from jsonb_array_elements(case when jsonb_typeof(p_definition -> 'pasos') = 'array'
                                                             then p_definition -> 'pasos' else '[]'::jsonb end) step(value)
               where jsonb_typeof(step.value) = 'object' and step.value ? 'plantilla') <> 1
             or exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> 'pasos') = 'array'
                                                                 then p_definition -> 'pasos' else '[]'::jsonb end) step(value)
                         where not (jsonb_typeof(step.value) = 'object'
                                    and (step.value ? 'plantilla' or step.value ? 'esperar' or step.value ? 'etiquetar')))
             -- etiquetar marca clienta: solo con la compra (la regla 5 del esquema, tambien en la base).
             or (public._flow_engine_native_trigger(p_definition) is distinct from 'compra'
                 and exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> 'pasos') = 'array'
                                                                     then p_definition -> 'pasos' else '[]'::jsonb end) step(value)
                              where jsonb_typeof(step.value) = 'object' and step.value ? 'etiquetar'))
            then 'motor_nativo:forma'
    end
$function$;

-- ===========================================================================
-- Bloque 3. El permiso del carrito.
-- ===========================================================================

-- El motivo del motor para un resultado de _portable_consented_intent_reason,
-- o null si la intencion da el permiso. Los mismos del carrito del motor.
create function public._flow_engine_native_cart_reason(
    p_purchase_intent_id uuid, p_contact_id uuid, p_destination text)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when consent.reason_code = 'consented_intent_ok' then null
        when consent.reason_code like '%prior_opt_out' then 'baja'
        when consent.reason_code like '%not_live' then 'ya_no_entra'
        else 'no_entra:permiso'
    end
      from public._portable_consented_intent_reason(p_purchase_intent_id, p_contact_id, p_destination) consent
$function$;

-- El permiso formulario de un carrito: la intencion que la admision portable
-- correlaciono con ESTE evento (no la busca por contacto: una intencion de
-- otra oferta o de otro telefono no cuenta), de la instancia y de la oferta
-- del evento, con consentimiento vigente para ese destino. reason es null si
-- entra, o baja, ya_no_entra o no_entra:permiso.
create function public._flow_engine_native_cart_audience(
    p_webhook_event_id uuid, p_contact_id uuid, p_offer text, p_destination text, p_tenant_ref text,
    out purchase_intent_id uuid, out precheckout_submission_id uuid, out reason text)
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_intent public.purchase_intents%rowtype;
    v_consent text;
    v_submission uuid;
begin
    purchase_intent_id := null;
    precheckout_submission_id := null;
    select intent.* into v_intent
      from public.hotmart_purchase_intent_correlations correlation
      join public.purchase_intents intent on intent.id = correlation.purchase_intent_id
     where correlation.webhook_event_id = p_webhook_event_id
       and correlation.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
       and correlation.outcome = 'resolved'
     limit 1;
    -- La intencion es de la instancia, de la oferta del evento y del producto del evento
    -- (el funnel del binding portable del carrito).
    if not found or p_offer is null or v_intent.tenant_ref is distinct from p_tenant_ref
       or v_intent.offer_ref is distinct from p_offer
       or not exists (select 1 from public.commercial_ally_hotmart_event_bindings event_binding
                       where event_binding.webhook_event_id = p_webhook_event_id
                         and event_binding.tenant_ref = v_intent.tenant_ref
                         and event_binding.funnel_ref = v_intent.funnel_ref) then
        reason := 'no_entra:permiso';
        return;
    end if;
    select consent.reason_code, consent.precheckout_submission_id into v_consent, v_submission
      from public._portable_consented_intent_reason(v_intent.id, p_contact_id, p_destination) consent;
    if v_consent is distinct from 'consented_intent_ok' then
        reason := case
            when v_consent like '%prior_opt_out' then 'baja'
            when v_consent like '%not_live' then 'ya_no_entra'
            else 'no_entra:permiso'
        end;
        return;
    end if;
    purchase_intent_id := v_intent.id;
    precheckout_submission_id := v_submission;
    reason := null;
end;
$function$;

-- ===========================================================================
-- Bloque 4. Los reemplazos sobre la definicion vigente.
-- ===========================================================================

do $patch_admission$
declare
    c_admit constant text :=
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)';
begin
    -- Las dos variables de la intencion del carrito (nulas con la compra).
    perform pg_temp.flow_engine_native_cart_patch(c_admit,
        E'    v_now timestamptz := clock_timestamp();\n',
        E'    v_now timestamptz := clock_timestamp();\n'
        || E'    -- flow_engine_native_cart_vars: la intencion que da el permiso del carrito.\n'
        || E'    v_audience_intent uuid;\n'
        || E'    v_audience_submission uuid;\n',
        'flow_engine_native_cart_vars');

    -- El permiso.
    perform pg_temp.flow_engine_native_cart_patch(c_admit,
        E'    -- El permiso: con compra, la evidencia es este evento de compra.\n'
        || E'    if public._flow_engine_native_trigger(v_definition.definition) is distinct from ''compra''\n'
        || E'       or p_event.event_type is distinct from ''PURCHASE_APPROVED'' then\n'
        || E'        v_reason := ''no_entra:permiso'';\n'
        || E'    end if;\n',
        E'    -- flow_engine_native_cart_permission: begin\n'
        || E'    -- El permiso (20261028000200). Con la compra, la evidencia es este evento\n'
        || E'    -- de compra. Con el carrito (permiso formulario), la intencion con\n'
        || E'    -- consentimiento que la admision portable correlaciono con ESTE carrito.\n'
        || E'    if public._flow_engine_native_trigger(v_definition.definition) = ''carrito''\n'
        || E'       and p_event.event_type = ''PURCHASE_OUT_OF_SHOPPING_CART'' then\n'
        || E'        select audience.purchase_intent_id, audience.precheckout_submission_id, audience.reason\n'
        || E'          into v_audience_intent, v_audience_submission, v_reason\n'
        || E'          from public._flow_engine_native_cart_audience(\n'
        || E'              p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref) audience;\n'
        || E'    elsif public._flow_engine_native_trigger(v_definition.definition) is distinct from ''compra''\n'
        || E'       or p_event.event_type is distinct from ''PURCHASE_APPROVED'' then\n'
        || E'        v_reason := ''no_entra:permiso'';\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_cart_permission: end\n',
        'flow_engine_native_cart_permission: begin');

    -- La autorizacion: con el carrito la concede la intencion, como el pago
    -- rechazado portable; con la compra, la compra, como antes.
    perform pg_temp.flow_engine_native_cart_patch(c_admit,
        E'        values (p_contact_id, ''whatsapp'', ''cart_recovery'', ''allowed'', ''hotmart'',\n'
        || E'            jsonb_build_object(''reason'', ''purchase_approved'', ''webhook_event_id'', p_event.id,\n'
        || E'                               ''recovery_case_id'', v_case_id),\n'
        || E'            v_now);\n',
        E'        -- flow_engine_native_cart_grant: el carrito concede con su intencion (20261028000200).\n'
        || E'        values (p_contact_id, ''whatsapp'', ''cart_recovery'', ''allowed'',\n'
        || E'            case when v_audience_intent is null then ''hotmart'' else ''system'' end,\n'
        || E'            case when v_audience_intent is null\n'
        || E'                 then jsonb_build_object(''reason'', ''purchase_approved'', ''webhook_event_id'', p_event.id,\n'
        || E'                                         ''recovery_case_id'', v_case_id)\n'
        || E'                 else jsonb_build_object(''reason'', ''precheckout_whatsapp_consent'',\n'
        || E'                                         ''purchase_intent_id'', v_audience_intent,\n'
        || E'                                         ''precheckout_submission_id'', v_audience_submission,\n'
        || E'                                         ''webhook_event_id'', p_event.id,\n'
        || E'                                         ''recovery_case_id'', v_case_id) end,\n'
        || E'            v_now);\n',
        'flow_engine_native_cart_grant');

    -- La fila del recorrido, con la intencion del carrito.
    perform pg_temp.flow_engine_native_cart_patch(c_admit,
        E'    insert into public.flow_engine_native_runs (recovery_case_id, trigger_event, offer_code,\n'
        || E'        chatwoot_account_id, chatwoot_inbox_id)\n'
        || E'    values (v_case_id, public._flow_engine_native_trigger(v_definition.definition), p_offer,\n'
        || E'        p_account_id, p_inbox_id);\n',
        E'    -- flow_engine_native_cart_run: la intencion del carrito (20261028000200).\n'
        || E'    insert into public.flow_engine_native_runs (recovery_case_id, trigger_event, offer_code,\n'
        || E'        chatwoot_account_id, chatwoot_inbox_id, audience_purchase_intent_id,\n'
        || E'        audience_precheckout_submission_id)\n'
        || E'    values (v_case_id, public._flow_engine_native_trigger(v_definition.definition), p_offer,\n'
        || E'        p_account_id, p_inbox_id, v_audience_intent, v_audience_submission);\n',
        'flow_engine_native_cart_run');

    -- El arranque: la intencion otra vez, despues de la cohorte y antes de los
    -- topes, como el carrito del motor (20261022000100).
    perform pg_temp.flow_engine_native_cart_patch(
        'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)',
        E'        return query select ''rechazado''::text, ''fuera_de_cohorte''::text, null::uuid, p_control.generation, false;\n'
        || E'        return;\n'
        || E'    end if;\n',
        E'        return query select ''rechazado''::text, ''fuera_de_cohorte''::text, null::uuid, p_control.generation, false;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_cart_recheck: begin\n'
        || E'    -- El permiso del carrito otra vez (20261028000200), con la intencion FOR\n'
        || E'    -- SHARE: la puerta lo mira antes de cada envio.\n'
        || E'    if v_native.trigger_event = ''carrito'' then\n'
        || E'        perform 1 from public.purchase_intents intent\n'
        || E'         where intent.id = v_native.audience_purchase_intent_id\n'
        || E'         for share;\n'
        || E'        if public._flow_engine_native_cart_reason(v_native.audience_purchase_intent_id, p_case.contact_id,\n'
        || E'               v_identity.external_user_id) is not null then\n'
        || E'            return query select ''rechazado''::text,\n'
        || E'                public._flow_engine_native_cart_reason(v_native.audience_purchase_intent_id,\n'
        || E'                    p_case.contact_id, v_identity.external_user_id),\n'
        || E'                null::uuid, p_control.generation, false;\n'
        || E'            return;\n'
        || E'        end if;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_cart_recheck: end\n',
        'flow_engine_native_cart_recheck: begin');

    -- La simulacion de un item nuevo cuenta tambien los carritos.
    perform pg_temp.flow_engine_native_cart_patch(
        'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)',
        E'    if p_trigger is distinct from ''compra'' then\n',
        E'    -- flow_engine_native_cart_counts: la compra o el carrito (20261028000200).\n'
        || E'    if p_trigger is null or p_trigger not in (''compra'', ''carrito'') then\n',
        'flow_engine_native_cart_counts');
    perform pg_temp.flow_engine_native_cart_patch(
        'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)',
        E'                                   public._whatsapp_phone_canonical(e.payload #>> ''{data,buyer,checkout_phone}''),\n',
        E'                                   -- flow_engine_native_cart_phone: el carrito trae buyer.phone.\n'
        || E'                                   public._whatsapp_phone_canonical(\n'
        || E'                                       e.payload #>> (case p_trigger when ''compra'' then ''{data,buyer,checkout_phone}''\n'
        || E'                                                                     else ''{data,buyer,phone}'' end)::text[]),\n',
        'flow_engine_native_cart_phone');
    perform pg_temp.flow_engine_native_cart_patch(
        'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)',
        E'     where e.source = ''hotmart'' and e.event_type = ''PURCHASE_APPROVED''\n',
        E'     -- flow_engine_native_cart_event: el evento del disparador.\n'
        || E'     where e.source = ''hotmart''\n'
        || E'       and e.event_type = case p_trigger when ''compra'' then ''PURCHASE_APPROVED''\n'
        || E'                                         else ''PURCHASE_OUT_OF_SHOPPING_CART'' end\n',
        'flow_engine_native_cart_event');

    -- La historia del AE: el evento que arranco un recorrido del carrito.
    perform pg_temp.flow_engine_native_cart_patch(
        'public.read_automation_expert_native_history_v1(jsonb,uuid)',
        E'case runs.trigger_event when ''compra'' then ''PURCHASE_APPROVED'' end',
        E'case runs.trigger_event when ''compra'' then ''PURCHASE_APPROVED''\n'
        || E'                                    -- flow_engine_native_cart_history (20261028000200)\n'
        || E'                                    when ''carrito'' then ''PURCHASE_OUT_OF_SHOPPING_CART'' end',
        'flow_engine_native_cart_history');
end;
$patch_admission$;

-- ===========================================================================
-- Bloque 5. El contexto del paso con el permiso de la version fijada.
-- ===========================================================================

do $context_shape$
declare
    v_definition text := pg_get_functiondef('public.flow_engine_execution_context_v1(uuid)'::regprocedure);
begin
    -- La definicion vigente es la de 20261022000100 con la forma nativa de
    -- 20261024000200: sin ella, otra migracion la cambio y esto no la conoce.
    if position('public._flow_engine_native_run_steps(run.recovery_case_id)' in v_definition) = 0
       or position('send.boton, run.legacy_dispatch_ok' in v_definition) = 0
       or position('entry_permission' in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_execution_context_definition';
    end if;
end;
$context_shape$;

drop function public.flow_engine_execution_context_v1(uuid);

-- La de 20261022000100 + 20261024000200, con entry_permission al final: el
-- entra.permiso de la version fijada del recorrido (flow_engine_native_cart).
create function public.flow_engine_execution_context_v1(p_action_id uuid)
returns table (flow_id uuid, version integer, product_ref text, flow_key text, step_key text,
               send_ordinal integer, template_name text, template_language text, template_variables text[],
               template_button jsonb, legacy_dispatch_ok boolean, entry_permission text)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select run.flow_id, run.version, run.product_ref, run.flow_key, action_row.step_key,
           position_row.ordinal, send.plantilla, send.idioma,
           array(select jsonb_array_elements_text(coalesce(send.variables, '[]'::jsonb))),
           send.boton, run.legacy_dispatch_ok,
           -- flow_engine_native_cart_permission: el permiso de la version fijada (20261028000200).
           definition_row.definition #>> '{entra,permiso}'
      from public.scheduled_actions action_row
      join public.flow_engine_runs run on run.recovery_case_id = action_row.recovery_case_id
      join public.flow_definitions definition_row
        on definition_row.flow_id = run.flow_id and definition_row.version = run.version
      cross join lateral (
          select shape.step_index::integer as ordinal
            -- flow_engine_native_steps: la forma del disparador en un recorrido nativo.
            from jsonb_array_elements(coalesce(public._flow_engine_legacy_steps(run.legacy_kind),
                                               public._flow_engine_native_run_steps(run.recovery_case_id)))
                 with ordinality as shape(step, step_index)
           where shape.step ->> 'step_key' = action_row.step_key
      ) position_row
      join lateral public._flow_engine_sends(definition_row.definition) send
        on send.ordinal = position_row.ordinal
     where action_row.id = p_action_id
$function$;

-- ===========================================================================
-- Los permisos. Las funciones nuevas, de nadie de la API; el contexto, otra
-- vez solo de service_role.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._flow_engine_native_cart_reason(uuid,uuid,text)',
        'public._flow_engine_native_cart_audience(uuid,uuid,text,text,text)',
        'public.flow_engine_execution_context_v1(uuid)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.flow_engine_execution_context_v1(uuid) to service_role;
    end if;
end;
$acl$;

commit;
