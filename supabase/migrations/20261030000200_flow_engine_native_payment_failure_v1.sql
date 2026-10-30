-- flow-engine-native-payment-failure-v1: el motor nativo arranca tambien con
-- el pago rechazado (E-207, M1b, el paso P18 del mapa de M16).
--
-- Por que. El Automation Expert solo crea flujos nativos, y el nativo sabia la
-- compra y el carrito: el pago rechazado de Maribel (el scope propio
-- maribel-pago-rechazado y la politica maribel-pago-rechazado-un-toque v1:
-- gracia 15 min, 09:00-21:00 CDMX, vence a 1 dia, 20 por dia y 1.000 en total,
-- un envio con la plantilla de WABA_PAYMENT_FAILURE_TEMPLATE_NAME) no cabia.
-- Contrato: docs/contracts/flow-engine-v2.md, seccion 23 (2.11.0).
--
-- Que cambia, por bloques:
--
-- 1. flow_engine_native_runs: trigger_event admite pago_fallido, y la
--    intencion con consentimiento (las dos columnas de 20261028000200) es
--    obligatoria tambien con el pago rechazado.
-- 2. Lo que el motor nativo ejecuta, reemplazado entero (son de una linea):
--    _flow_engine_native_trigger (compra, carrito o pago_fallido) y
--    _flow_engine_native_steps (nativo:pago_fallido, un envio con las
--    variables de nombre y producto, la forma de la compra y del carrito).
--    _flow_engine_native_unsupported no cambia: con todo disparador que no
--    es la compra pide el permiso formulario, y etiquetar solo con la compra.
-- 3. El permiso del pago rechazado: _flow_engine_native_payment_failure_audience,
--    el mismo criterio que plan_portable_payment_failure_recovery: la intencion
--    con la que la admision portable correlaciono ESTE rechazo
--    (commercial_ally_payment_failure_details resuelto), de la instancia, del
--    funnel, del producto y de la oferta del evento, con el consentimiento de
--    hoy (_portable_consented_intent_reason). Un cancelado que no es un
--    rechazo (payment_failure_refusal_kind = not_a_refusal: un pendiente de un
--    medio que no es tarjeta ni PayPal) da no_es_un_rechazo, como
--    payment_failure_not_a_refusal del legado.
-- 4. Reemplazos contados sobre la definicion VIGENTE:
--    admit_flow_engine_event_v1 (20261024000200): la forma del evento acepta
--      tipo pago_fallido, y el evento persistido tiene que ser PURCHASE_CANCELED;
--    _flow_engine_admit_native_flow (20261024000200 + 20261026000200 +
--      20261028000200 + 20261030000100):
--      - el permiso del pago rechazado (bloque 3);
--      - el doble caso (P12 del mapa, la misma regla de M1a): con el pago
--        rechazado, si la persona tiene un caso del pago rechazado del legado
--        (context.trigger_kind = payment_failure) del producto, de cualquier
--        oferta, que el planificador del legado tomaria (abierto, o cerrado con
--        mensaje, compra o persona dentro de las 72 h), no arranca:
--        caso_del_legado_en_curso, y el rechazo vuelve al legado, que hace lo
--        de hoy;
--      - el rechazo que llega con un recorrido del mismo flujo en curso
--        (ya_en_curso) corre el primer envio a la gracia contada desde ESTE
--        rechazo, como el legado (payment_failure_variants, 20261019000200:
--        un solo mensaje, el del ultimo rechazo). Solo con el pago rechazado.
--    _flow_engine_authorize_native_tail (20261028000200): la puerta vuelve a
--      mirar la intencion antes del envio tambien con el pago rechazado;
--    read_flow_engine_native_trigger_counts_v1: la simulacion cuenta los
--      rechazos (PURCHASE_CANCELED que son un rechazo);
--    read_automation_expert_native_history_v1: el evento que arranco un
--      recorrido del pago rechazado dice PURCHASE_CANCELED;
--    plan_cart_recovery (20261030000100): el doble caso del lado del carrito
--      del legado mira tambien un caso abierto de un pago rechazado nativo. Hoy
--      un pago rechazado del legado abierto frena el carrito de la misma oferta
--      (el carrito cae en su caso y aborta con pilot_case_binding_conflict); el
--      caso nativo nace con offer_code nulo y el carrito no lo veia;
--    _flow_change_new_flow_blocker (20261024000600): D1 del mapa de M16, un
--      item nuevo con la clave de un flujo del legado (precheckout, carrito,
--      pago_fallido, reactivacion, descuento) no se publica:
--      flow_key_reserved_for_legacy. El validador lo frena antes, con el mismo
--      codigo;
--    plan_payment_failure_recovery (el planificador base del pago rechazado,
--      debajo de plan_portable_payment_failure_recovery): el doble caso del
--      lado del legado. Con un caso de un pago rechazado NATIVO de la persona
--      que el legado habria tomado (abierto, o cerrado con mensaje, compra o
--      persona dentro de las 72 h), de cualquier oferta, aborta con 55000
--      pilot_scope_rejected / flujo_nativo_en_curso y no escribe nada. Es la
--      vuelta atras del nativo pausado o apagado.
--
-- Lo que no cambia. Sin un flujo nativo del pago rechazado no hay casos
-- nativos del pago rechazado: las dos ramas del legado nunca dan verdadero, y
-- la admision nativa no se llama (el bridge solo la llama con un flujo nativo
-- del pago rechazado del producto prendido o en cohorte). El pago rechazado
-- del legado (ATT1 con las variantes B1/B2; Maribel con su scope propio), la
-- compra y el carrito, iguales (validate_flow_engine_native_payment_failure.mjs).
--
-- Lo que no hace: no crea roles, no cambia el ACL de lo que ya existe y no
-- toca filas. Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.flow_engine_native_runs') is null
       or to_regprocedure('public._flow_engine_native_cart_audience(uuid,uuid,text,text,text)') is null
       or to_regprocedure('public.payment_failure_refusal_kind(text,text)') is null
       or to_regclass('public.commercial_ally_payment_failure_details') is null
       or position('flow_engine_native_legacy_double_case' in pg_get_functiondef(
              'public.plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_missing';
    end if;
    if to_regprocedure('public._flow_engine_native_payment_failure_audience(uuid,uuid,text,text,text)') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla, con la misma verificacion para todos.
create function pg_temp.flow_engine_native_payment_failure_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_payment_failure_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_definition_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- Bloque 1. La fila del recorrido.
-- ===========================================================================

alter table public.flow_engine_native_runs
    drop constraint flow_engine_native_runs_trigger_event_check,
    add constraint flow_engine_native_runs_trigger_event_check
        check (trigger_event in ('compra', 'carrito', 'pago_fallido')),
    drop constraint flow_engine_native_runs_audience_by_trigger,
    add constraint flow_engine_native_runs_audience_by_trigger check (
        (trigger_event in ('carrito', 'pago_fallido')
         and audience_purchase_intent_id is not null and audience_precheckout_submission_id is not null)
        or (trigger_event not in ('carrito', 'pago_fallido')
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
    -- flow_engine_native_payment_failure: y desde 20261030000200 el pago rechazado.
    select case
        when p_definition -> 'dispara' = '{"evento": "compra"}'::jsonb then 'compra'
        when p_definition -> 'dispara' = '{"evento": "carrito"}'::jsonb then 'carrito'
        when p_definition -> 'dispara' = '{"evento": "pago_fallido"}'::jsonb then 'pago_fallido'
    end
$function$;

-- La forma de los pasos de un flujo nativo: sale del disparador. Los tres son
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
        -- flow_engine_native_payment_failure: el pago rechazado de un toque (20261030000200).
        when 'nativo:pago_fallido' then
            '[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "variables_de": ["nombre", "producto"], "coupon_variables": null}]'::jsonb
    end
$function$;

-- ===========================================================================
-- Bloque 3. El permiso del pago rechazado.
-- ===========================================================================

-- El permiso formulario de un pago rechazado: la intencion que la admision
-- portable correlaciono con ESTE evento (commercial_ally_payment_failure_details
-- resuelto; nunca la busca por contacto), de la instancia, del funnel, del
-- producto y de la oferta del evento (la procedencia del binding portable),
-- con consentimiento vigente para ese destino. Los controles de
-- plan_portable_payment_failure_recovery, en el mismo orden. reason es null si
-- entra; si no, no_es_un_rechazo, baja, ya_no_entra o no_entra:permiso.
create function public._flow_engine_native_payment_failure_audience(
    p_webhook_event_id uuid, p_contact_id uuid, p_offer text, p_destination text, p_tenant_ref text,
    out purchase_intent_id uuid, out precheckout_submission_id uuid, out reason text)
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_details public.commercial_ally_payment_failure_details%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_event public.webhook_events%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_consent text;
    v_submission uuid;
begin
    purchase_intent_id := null;
    precheckout_submission_id := null;
    reason := 'no_entra:permiso';
    select event_row.* into v_event
      from public.webhook_events event_row
     where event_row.id = p_webhook_event_id and event_row.source = 'hotmart'
       and event_row.event_type = 'PURCHASE_CANCELED';
    if not found then
        return;
    end if;
    select provenance.* into v_provenance
      from public.commercial_ally_hotmart_event_bindings provenance
     where provenance.webhook_event_id = p_webhook_event_id;
    if not found or v_provenance.tenant_ref is distinct from p_tenant_ref or p_offer is null
       or v_provenance.offer_ref is distinct from p_offer then
        return;
    end if;
    select details.* into v_details
      from public.commercial_ally_payment_failure_details details
     where details.webhook_event_id = p_webhook_event_id
       and details.trigger_kind = 'payment_failure'
       and details.correlation_outcome = 'resolved'
       and details.purchase_intent_id is not null;
    if not found then
        return;
    end if;
    -- Un pendiente de un medio que no es tarjeta ni PayPal que se cancelo no es un rechazo.
    if public.payment_failure_refusal_kind(v_details.refusal_reason,
           v_event.payload #>> '{data,purchase,payment,type}') = 'not_a_refusal' then
        reason := 'no_es_un_rechazo';
        return;
    end if;
    select intent.* into v_intent
      from public.purchase_intents intent
     where intent.id = v_details.purchase_intent_id
       and intent.tenant_ref = v_provenance.tenant_ref
       and intent.funnel_ref = v_provenance.funnel_ref
       and intent.product_ref = v_provenance.purchase_intent_product_ref
       and intent.offer_ref = v_provenance.offer_ref
       and public._whatsapp_phone_canonical(intent.normalized_phone)
           = public._whatsapp_phone_canonical(p_destination);
    if not found then
        return;
    end if;
    -- El contacto tiene ese movil (payment_failure_contact_mismatch del legado).
    if not exists (
        select 1 from public.contact_points point
         where point.contact_id = p_contact_id and point.type = 'phone'
           and point.normalized_value = any(public._whatsapp_phone_variants(v_intent.normalized_phone))
           and point.source in ('hotmart', 'system')) then
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

do $patch$
declare
    c_event constant text := 'public.admit_flow_engine_event_v1(jsonb,jsonb)';
    c_admit constant text :=
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)';
    c_counts constant text := 'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)';
begin
    -- La forma del evento: el tipo pago_fallido.
    perform pg_temp.flow_engine_native_payment_failure_patch(c_event,
        E'       or coalesce(p_flow_event ->> ''tipo'', '''') not in (''compra'', ''carrito'')\n',
        E'       -- flow_engine_native_payment_failure_type: y el pago rechazado (20261030000200).\n'
        || E'       or coalesce(p_flow_event ->> ''tipo'', '''') not in (''compra'', ''carrito'', ''pago_fallido'')\n',
        'flow_engine_native_payment_failure_type');
    -- El evento persistido del tipo.
    perform pg_temp.flow_engine_native_payment_failure_patch(c_event,
        E'                                                    else ''PURCHASE_OUT_OF_SHOPPING_CART'' end)\n',
        E'                                                    -- flow_engine_native_payment_failure_event\n'
        || E'                                                    when ''pago_fallido'' then ''PURCHASE_CANCELED''\n'
        || E'                                                    else ''PURCHASE_OUT_OF_SHOPPING_CART'' end)\n',
        'flow_engine_native_payment_failure_event');

    -- El doble caso con el pago rechazado del legado, despues del del carrito.
    perform pg_temp.flow_engine_native_payment_failure_patch(c_admit,
        E'    -- flow_engine_native_legacy_double_case: end\n',
        E'    -- flow_engine_native_legacy_double_case: end\n'
        || E'\n'
        || E'    -- flow_engine_native_payment_failure_double_case: begin\n'
        || E'    -- El doble caso con el pago rechazado (20261030000200, P18 del mapa de M16,\n'
        || E'    -- la regla de M1a). Un caso del pago rechazado del legado de la persona y\n'
        || E'    -- del producto, de cualquier oferta, que el planificador del legado\n'
        || E'    -- tomaria (abierto, o cerrado con mensaje, compra o persona dentro de las\n'
        || E'    -- 72 h: payment_failure_variants, 20261019000200): el rechazo es del legado,\n'
        || E'    -- que decide como hoy (el bridge lo trata como sin_reclamo).\n'
        || E'    if public._flow_engine_native_trigger(v_definition.definition) = ''pago_fallido''\n'
        || E'       and exists (\n'
        || E'           select 1\n'
        || E'             from public.recovery_cases legacy_case\n'
        || E'            where legacy_case.contact_id = p_contact_id\n'
        || E'              and legacy_case.source = p_event.source\n'
        || E'              and legacy_case.external_product_id = (\n'
        || E'                  select binding.hotmart_product_id::text\n'
        || E'                    from public.commercial_ally_runtime_bindings binding\n'
        || E'                   where binding.tenant_ref = p_control.tenant_ref and binding.ally_ref = p_control.ally_ref\n'
        || E'                     and binding.funnel_ref = p_control.product_ref and binding.status = ''active''\n'
        || E'                   order by binding.binding_version desc\n'
        || E'                   limit 1)\n'
        || E'              and legacy_case.context ->> ''trigger_kind'' = ''payment_failure''\n'
        || E'              and (legacy_case.status in (''grace_period'', ''active'', ''paused'')\n'
        || E'                   or (legacy_case.status in (''sequence_exhausted'', ''won'', ''escalated'')\n'
        || E'                       and legacy_case.created_at > p_moment - interval ''72 hours''))) then\n'
        || E'        perform public._flow_engine_record_start_rejection(\n'
        || E'            p_control.flow_id, v_definition.version, p_event.id, ''admision'', ''caso_del_legado_en_curso'');\n'
        || E'        return query select ''motor_no_arranca''::text, ''caso_del_legado_en_curso''::text,\n'
        || E'            null::uuid, null::uuid, null::uuid, null::boolean;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_payment_failure_double_case: end\n',
        'flow_engine_native_payment_failure_double_case: begin');

    -- El permiso del pago rechazado, antes del de la compra.
    perform pg_temp.flow_engine_native_payment_failure_patch(c_admit,
        E'    elsif public._flow_engine_native_trigger(v_definition.definition) is distinct from ''compra''\n'
        || E'       or p_event.event_type is distinct from ''PURCHASE_APPROVED'' then\n',
        E'    -- flow_engine_native_payment_failure_permission: el pago rechazado (20261030000200),\n'
        || E'    -- con la intencion que la admision portable correlaciono con ESTE rechazo.\n'
        || E'    elsif public._flow_engine_native_trigger(v_definition.definition) = ''pago_fallido''\n'
        || E'       and p_event.event_type = ''PURCHASE_CANCELED'' then\n'
        || E'        select audience.purchase_intent_id, audience.precheckout_submission_id, audience.reason\n'
        || E'          into v_audience_intent, v_audience_submission, v_reason\n'
        || E'          from public._flow_engine_native_payment_failure_audience(\n'
        || E'              p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref) audience;\n'
        || E'    elsif public._flow_engine_native_trigger(v_definition.definition) is distinct from ''compra''\n'
        || E'       or p_event.event_type is distinct from ''PURCHASE_APPROVED'' then\n',
        'flow_engine_native_payment_failure_permission');

    -- Un rechazo con un recorrido del mismo flujo en curso: un solo mensaje, el
    -- del ultimo rechazo, a la gracia contada desde ese rechazo (como el legado).
    perform pg_temp.flow_engine_native_payment_failure_patch(c_admit,
        E'    -- La prioridad entre flujos del motor (relations_between_engine_flows_only):\n',
        E'    -- flow_engine_native_payment_failure_last_refusal: begin\n'
        || E'    -- El pago rechazado (20261030000200): un rechazo que llega con un recorrido\n'
        || E'    -- del mismo flujo en curso de la persona no abre otro, y el primer envio\n'
        || E'    -- pendiente pasa a la gracia contada desde este rechazo (nunca antes de lo\n'
        || E'    -- que ya tenia), como payment_failure_variants del legado: quien reintenta\n'
        || E'    -- la tarjeta recibe un solo mensaje, despues del ultimo intento.\n'
        || E'    if v_reason = ''ya_en_curso''\n'
        || E'       and public._flow_engine_native_trigger(v_definition.definition) = ''pago_fallido'' then\n'
        || E'        -- El caso en curso, FOR UPDATE como el legado (evento, contacto, caso).\n'
        || E'        perform 1\n'
        || E'          from public.recovery_cases rc\n'
        || E'          join public.flow_engine_runs run on run.recovery_case_id = rc.id\n'
        || E'          join public.flow_engine_native_runs native on native.recovery_case_id = rc.id\n'
        || E'         where run.flow_id = p_control.flow_id and run.person_ref = p_contact_id\n'
        || E'           and rc.status in (''grace_period'', ''active'', ''paused'')\n'
        || E'           and (v_definition.definition #>> ''{topes,repeticion,una_por}'' = ''persona''\n'
        || E'                or native.offer_code is not distinct from p_offer)\n'
        || E'         order by rc.id\n'
        || E'           for update of rc;\n'
        || E'        update public.scheduled_actions action_row\n'
        || E'           set due_at = greatest(action_row.due_at, p_moment + policy_row.grace_period),\n'
        || E'               next_attempt_at = case when action_row.next_attempt_at is null then null\n'
        || E'                                      else greatest(action_row.next_attempt_at,\n'
        || E'                                                    p_moment + policy_row.grace_period) end\n'
        || E'          from public.flow_engine_runs run\n'
        || E'          join public.recovery_cases rc on rc.id = run.recovery_case_id\n'
        || E'          join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id\n'
        || E'          join public.followup_sequences sequence_row\n'
        || E'            on sequence_row.recovery_case_id = run.recovery_case_id and sequence_row.status = ''active''\n'
        || E'          join public.followup_policy_versions policy_row\n'
        || E'            on policy_row.policy_key = ''flujo:'' || run.flow_id::text and policy_row.version = run.version\n'
        || E'         where run.flow_id = p_control.flow_id and run.person_ref = p_contact_id\n'
        || E'           and rc.status in (''grace_period'', ''active'', ''paused'')\n'
        || E'           and (v_definition.definition #>> ''{topes,repeticion,una_por}'' = ''persona''\n'
        || E'                or native.offer_code is not distinct from p_offer)\n'
        || E'           and action_row.recovery_case_id = run.recovery_case_id\n'
        || E'           and action_row.followup_sequence_id = sequence_row.id\n'
        || E'           and action_row.anchor_type = ''flow_engine_event''\n'
        || E'           and action_row.status in (''pending'', ''deferred'', ''retryable_failed'');\n'
        || E'        -- El rechazo queda en el caso, como en el legado (recovery_case_events).\n'
        || E'        insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)\n'
        || E'        select run.recovery_case_id, p_event.id, ''payment_failure'', p_moment\n'
        || E'          from public.flow_engine_runs run\n'
        || E'          join public.recovery_cases rc on rc.id = run.recovery_case_id\n'
        || E'          join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id\n'
        || E'         where run.flow_id = p_control.flow_id and run.person_ref = p_contact_id\n'
        || E'           and rc.status in (''grace_period'', ''active'', ''paused'')\n'
        || E'           and (v_definition.definition #>> ''{topes,repeticion,una_por}'' = ''persona''\n'
        || E'                or native.offer_code is not distinct from p_offer)\n'
        || E'         order by run.started_at desc\n'
        || E'         limit 1\n'
        || E'        on conflict do nothing;\n'
        || E'    elsif v_reason = ''ya_disparado''\n'
        || E'       and public._flow_engine_native_trigger(v_definition.definition) = ''pago_fallido'' then\n'
        || E'        -- Un rechazo dentro de otra_despues_de de un recorrido que ya termino\n'
        || E'        -- (en Maribel, las 72 h): se suma a ese caso sin mensaje, como el legado.\n'
        || E'        insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)\n'
        || E'        select run.recovery_case_id, p_event.id, ''payment_failure'', p_moment\n'
        || E'          from public.flow_engine_runs run\n'
        || E'          join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id\n'
        || E'         where run.flow_id = p_control.flow_id and run.person_ref = p_contact_id\n'
        || E'           and (v_definition.definition #>> ''{topes,repeticion,una_por}'' = ''persona''\n'
        || E'                or native.offer_code is not distinct from p_offer)\n'
        || E'         order by run.started_at desc\n'
        || E'         limit 1\n'
        || E'        on conflict do nothing;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_payment_failure_last_refusal: end\n'
        || E'\n'
        || E'    -- La prioridad entre flujos del motor (relations_between_engine_flows_only):\n',
        'flow_engine_native_payment_failure_last_refusal: begin');

    -- El arranque: la intencion otra vez, tambien con el pago rechazado.
    perform pg_temp.flow_engine_native_payment_failure_patch(
        'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)',
        E'    if v_native.trigger_event = ''carrito'' then\n',
        E'    -- flow_engine_native_payment_failure_recheck: y el pago rechazado (20261030000200).\n'
        || E'    if v_native.trigger_event in (''carrito'', ''pago_fallido'') then\n',
        'flow_engine_native_payment_failure_recheck');

    -- La simulacion de un item nuevo cuenta tambien los rechazos.
    perform pg_temp.flow_engine_native_payment_failure_patch(c_counts,
        E'    if p_trigger is null or p_trigger not in (''compra'', ''carrito'') then\n',
        E'    -- flow_engine_native_payment_failure_counts: y el pago rechazado (20261030000200).\n'
        || E'    if p_trigger is null or p_trigger not in (''compra'', ''carrito'', ''pago_fallido'') then\n',
        'flow_engine_native_payment_failure_counts');
    perform pg_temp.flow_engine_native_payment_failure_patch(c_counts,
        E'                                       e.payload #>> (case p_trigger when ''compra'' then ''{data,buyer,checkout_phone}''\n'
        || E'                                                                     else ''{data,buyer,phone}'' end)::text[]),\n',
        E'                                       -- flow_engine_native_payment_failure_phone: el rechazo trae checkout_phone.\n'
        || E'                                       e.payload #>> (case p_trigger when ''carrito'' then ''{data,buyer,phone}''\n'
        || E'                                                                     else ''{data,buyer,checkout_phone}'' end)::text[]),\n',
        'flow_engine_native_payment_failure_phone');
    perform pg_temp.flow_engine_native_payment_failure_patch(c_counts,
        E'       and e.event_type = case p_trigger when ''compra'' then ''PURCHASE_APPROVED''\n'
        || E'                                         else ''PURCHASE_OUT_OF_SHOPPING_CART'' end\n',
        E'       -- flow_engine_native_payment_failure_kind: el rechazo, no un pendiente cancelado.\n'
        || E'       and e.event_type = case p_trigger when ''compra'' then ''PURCHASE_APPROVED''\n'
        || E'                                         when ''pago_fallido'' then ''PURCHASE_CANCELED''\n'
        || E'                                         else ''PURCHASE_OUT_OF_SHOPPING_CART'' end\n'
        || E'       and (p_trigger is distinct from ''pago_fallido''\n'
        || E'            or public.payment_failure_refusal_kind(e.payload #>> ''{data,purchase,payment,refusal_reason}'',\n'
        || E'                                                   e.payload #>> ''{data,purchase,payment,type}'')\n'
        || E'               <> ''not_a_refusal'')\n',
        'flow_engine_native_payment_failure_kind');

    -- La historia del AE: el evento que arranco un recorrido del pago rechazado.
    perform pg_temp.flow_engine_native_payment_failure_patch(
        'public.read_automation_expert_native_history_v1(jsonb,uuid)',
        E'                                    when ''carrito'' then ''PURCHASE_OUT_OF_SHOPPING_CART'' end',
        E'                                    when ''carrito'' then ''PURCHASE_OUT_OF_SHOPPING_CART''\n'
        || E'                                    -- flow_engine_native_payment_failure_history (20261030000200)\n'
        || E'                                    when ''pago_fallido'' then ''PURCHASE_CANCELED'' end',
        'flow_engine_native_payment_failure_history');

    -- El carrito del legado ve tambien un pago rechazado nativo abierto.
    perform pg_temp.flow_engine_native_payment_failure_patch(
        'public.plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
        E'           and native_case.context ->> ''flow_engine_trigger'' = ''carrito''\n',
        E'           -- flow_engine_native_payment_failure_cart: y un pago rechazado nativo\n'
        || E'           -- abierto (20261030000200): hoy un pago rechazado del legado abierto frena\n'
        || E'           -- el carrito de la misma oferta.\n'
        || E'           and native_case.context ->> ''flow_engine_trigger'' in (''carrito'', ''pago_fallido'')\n',
        'flow_engine_native_payment_failure_cart');

    -- D1 del mapa de M16: un flujo nativo no usa la clave de un flujo del legado
    -- (la lectura del AE descarta todos los nativos si uno la usa). Al publicar,
    -- antes de flow_already_registered. Las claves son FLOW_KEYS de
    -- src/bridge/automation_expert_read.py (un test las compara).
    perform pg_temp.flow_engine_native_payment_failure_patch(
        'public._flow_change_new_flow_blocker(text,text,jsonb)',
        E'    -- La identidad del control no lleva la aliada (flow_controls_identity).\n',
        E'    -- flow_engine_native_payment_failure_reserved_key: las claves de los flujos del legado (20261030000200).\n'
        || E'    if p_item ->> ''flujo'' in (''precheckout'', ''carrito'', ''pago_fallido'', ''reactivacion'', ''descuento'') then\n'
        || E'        return ''flow_key_reserved_for_legacy'';\n'
        || E'    end if;\n'
        || E'    -- La identidad del control no lleva la aliada (flow_controls_identity).\n',
        'flow_engine_native_payment_failure_reserved_key');

    -- El pago rechazado del legado no planifica sobre un pago rechazado nativo.
    perform pg_temp.flow_engine_native_payment_failure_patch(
        'public.plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
        E'    select rc.id, rc.status, rc.policy_key, rc.policy_version\n'
        || E'      into v_case_id, v_case_status, v_case_policy_key, v_case_policy_version\n'
        || E'    from public.recovery_cases rc\n'
        || E'    where rc.contact_id = p_contact_id\n'
        || E'      and rc.source = v_event.source\n'
        || E'      and rc.external_product_id = p_external_product_id\n'
        || E'      and rc.context ->> ''trigger_kind'' = ''payment_failure''\n',
        E'    -- flow_engine_native_payment_failure_legacy: begin\n'
        || E'    -- El doble caso del lado del legado (20261030000200, la regla de M1a): un\n'
        || E'    -- caso de un pago rechazado NATIVO de la persona, de la misma fuente y el\n'
        || E'    -- mismo producto, de cualquier oferta, que este planificador habria tomado\n'
        || E'    -- (abierto, o cerrado con mensaje, compra o persona dentro de las 72 h).\n'
        || E'    -- Nace con offer_code nulo y sin trigger_kind: la busqueda de abajo no lo\n'
        || E'    -- ve. Es la vuelta atras del nativo pausado o apagado. Lee solo\n'
        || E'    -- recovery_cases: esta funcion es security invoker.\n'
        || E'    if exists (\n'
        || E'        select 1\n'
        || E'          from public.recovery_cases native_case\n'
        || E'         where native_case.contact_id = p_contact_id\n'
        || E'           and native_case.source = v_event.source\n'
        || E'           and native_case.external_product_id = p_external_product_id\n'
        || E'           and native_case.policy_key like ''flujo:%''\n'
        || E'           and native_case.context ->> ''flow_engine_trigger'' = ''pago_fallido''\n'
        || E'           and (native_case.status in (''grace_period'', ''active'', ''paused'')\n'
        || E'                or (native_case.status in (''sequence_exhausted'', ''won'', ''escalated'')\n'
        || E'                    and native_case.created_at > p_failed_at - interval ''72 hours''))\n'
        || E'    ) then\n'
        || E'        raise exception using errcode = ''55000'', message = ''pilot_scope_rejected'',\n'
        || E'            detail = ''flujo_nativo_en_curso'';\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_payment_failure_legacy: end\n'
        || E'\n'
        || E'    select rc.id, rc.status, rc.policy_key, rc.policy_version\n'
        || E'      into v_case_id, v_case_status, v_case_policy_key, v_case_policy_version\n'
        || E'    from public.recovery_cases rc\n'
        || E'    where rc.contact_id = p_contact_id\n'
        || E'      and rc.source = v_event.source\n'
        || E'      and rc.external_product_id = p_external_product_id\n'
        || E'      and rc.context ->> ''trigger_kind'' = ''payment_failure''\n',
        'flow_engine_native_payment_failure_legacy: begin');
end;
$patch$;

-- ===========================================================================
-- Los permisos. La funcion nueva, de nadie de la API.
-- ===========================================================================

do $acl$
declare
    v_role text;
begin
    revoke all on function public._flow_engine_native_payment_failure_audience(uuid,uuid,text,text,text) from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function public._flow_engine_native_payment_failure_audience(uuid,uuid,text,text,text) from %I',
            v_role);
    end loop;
end;
$acl$;

commit;
