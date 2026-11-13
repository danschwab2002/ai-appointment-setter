-- e528-native-payment-slip-v1: la ficha de OXXO impresa y sin pagar, nativa
-- (E-528, C4 del mapa «el AE maneja todos los flujos», 2026-11-13; P25 de M16).
--
-- Por que. La ficha existe en el esquema (ficha_impresa, ficha_vencida, el link
-- ficha y medio_de_pago) y no en el motor nativo: un flujo de la ficha solo lo
-- ejecuta el legado de ATT1 (att1-ficha v3, C1 y C3), con su scope, su politica
-- y la plantilla del manifiesto. El AE no puede crear ni cambiar la ficha de una
-- instancia nueva (Maribel nunca la mando: sus plantillas no estan aprobadas).
--
-- La definicion objetivo (el diseno de P25, §2.2):
--   esperar 60m; C1 (nombre, producto, la tienda, el vencimiento; boton ficha);
--   esperar {vencimiento: {dias_antes: 0, a_las: "09:00"}}; C3 (nombre, la
--   tienda, producto; boton ficha); termina si compra o ficha_vencida, vence 4d.
--
-- El caso nativo de la ficha es una ficha para la secuencia: followup_sequences
-- .reason = 'payment_slip'. Con eso, sin tocar ninguna funcion del legado, la
-- reevaluacion no corta C3 cuando la persona contesta (20261019000800), no lo
-- frena con la conversacion resuelta (20261019000400), la adopcion no lo cuenta
-- como pendiente (admit_portable_inbound_commercial_case_v1) y el motor del
-- carrito no arranca mientras (no_arranca_mientras:ficha). Lo que lo distingue de
-- la ficha del legado es el ancla de su primer envio (flow_engine_event), el
-- recorrido nativo (flow_engine_native_runs.trigger_event = 'ficha_impresa') y su
-- lectura propia del contexto de la ficha (punto 10). Las cuatro funciones del
-- legado de la ficha (get_portable_payment_slip_send_context,
-- mark_portable_payment_slip_request_started, la reevaluacion y la adopcion) NO
-- cambian: la ficha de ATT1 corre igual, byte a byte (lo mide la prueba).
--
-- Que cambia:
--   1. _flow_engine_native_trigger: ficha_impresa.
--   2. _flow_engine_native_steps('nativo:ficha_impresa'): dos ranuras,
--      first_contact (C1: nombre, producto, medio_de_pago, vencimiento) y
--      native_touch_2 (C3: nombre, medio_de_pago, producto, vencimiento).
--   3. _flow_engine_native_payment_slip_due_day (nueva, inmutable): la espera
--      anclada de C3, exactamente {vencimiento: {dias_antes: 0, a_las: "09:00"}}
--      (D-P25-2: la hora es una constante del codigo, la de
--      payment_slip_reminders.PAYMENT_SLIP_STEPS del bridge).
--   4. _flow_engine_native_payment_slip_shape (nueva, inmutable): la forma de la
--      ficha: solo esperar y plantilla; antes de C1, solo duraciones (la
--      gracia); entre C1 y C3 exactamente la espera anclada; ninguna espera
--      despues; uno o dos envios; el boton, si esta, {link: ficha} sin cupon; y la
--      salida contesta no (contestar no corta C3).
--   5. _flow_engine_native_unsupported: con ficha_impresa, la forma del punto 4
--      (y antes, como siempre, el permiso formulario y la repeticion).
--   6. _flow_engine_compile_policy_core: con nativo:ficha_impresa, la espera
--      anclada despues de C1 compila como un minuto (el delay de C3 del legado,
--      publicar-ficha-v1.sql:28-29): el momento real lo decide el bridge con
--      payment_slip_step_wait (el dia del vencimiento a las 09:00 de la persona).
--   7. admit_flow_engine_event_v1: el tipo ficha_impresa con PURCHASE_BILLET_PRINTED.
--   8. _flow_engine_admit_native_flow, con ficha_impresa:
--      - el permiso: la intencion que la admision v2 de la ficha correlaciono con
--        ESTA ficha (_flow_engine_native_payment_slip_audience, nueva, el molde de
--        la del pago rechazado);
--      - el doble caso: una ficha del legado abierta de la persona y del producto
--        (context.trigger_kind = 'payment_slip', no nativa) gana: el nativo no
--        arranca (caso_del_legado_en_curso);
--      - la repeticion: una segunda ficha con el recorrido en curso no abre otro
--        caso; se suma al abierto como recovery_case_events 'payment_slip' (C1 y
--        C3 usan la mas reciente) sin adelantar ni atrasar nada (D-P25-5);
--      - el caso: la secuencia con reason 'payment_slip' y la ficha como evento
--        del caso (event_role 'payment_slip').
--   9. _flow_engine_authorize_native_tail: antes de cada envio, la intencion de la
--      ficha sigue con permiso, como el carrito y el pago rechazado.
--  10. get_flow_engine_native_payment_slip_context_v1 (nueva, service_role): la
--      ficha mas reciente del caso nativo de una accion con lease vigente (el link,
--      el codigo de barras, el medio y el pais), con la misma cerca y la misma
--      lectura que get_portable_payment_slip_send_context, solo para un caso con
--      recorrido nativo de la ficha.
--  11. flow_engine_native_runs: trigger_event admite ficha_impresa, y la ficha
--      lleva su intencion (audience_by_trigger, como el carrito).
--  12. Las lecturas (E-531): la simulacion de un flujo de la ficha cuenta las
--      fichas impresas del producto (read_flow_engine_native_trigger_counts_v1
--      con ficha_impresa: PURCHASE_BILLET_PRINTED, las personas por mail o por
--      checkout_phone); la historia del AE dice el evento que arranco el
--      recorrido (read_automation_expert_native_history_v1); y los resultados
--      (read_results_v1) cuentan el recorrido nativo de la ficha como el flujo
--      ficha_oxxo, el mismo nombre que la ficha del legado, con ejecutor motor.
--
-- Lo que NO cambia. Ningun flujo publicado tiene ficha_impresa en el motor nativo
-- (la base y el validador lo rechazaban), asi que nada de lo que corre en ATT1 o
-- Maribel cambia al aplicarla. El bridge manda la ficha nativa solo con
-- FLOW_ENGINE_NATIVE_PAYMENT_SLIP_ENABLED (apagado por defecto).
--
-- Como: el metodo de 20261112210000, reemplazos contados sobre la definicion
-- VIGENTE de cada funcion (despues de 20261112220000), cada uno con su marca, que
-- no puede estar. Correrla dos veces aborta con 55000. El bloque 11 toma ACCESS
-- EXCLUSIVE sobre flow_engine_native_runs: va al final, con lock_timeout.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.admit_flow_engine_event_v1(jsonb,jsonb)') is null
       or to_regprocedure('public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)') is null
       or to_regprocedure('public._flow_engine_native_trigger(jsonb)') is null
       or to_regprocedure('public._flow_engine_native_steps(text)') is null
       or to_regprocedure('public._flow_engine_native_unsupported(jsonb)') is null
       or to_regprocedure('public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)') is null
       or to_regprocedure('public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)') is null
       or to_regprocedure('public._portable_consented_intent_reason(uuid,uuid,text)') is null
       or to_regclass('public.commercial_ally_payment_slip_details') is null
       or position('e483_native_audience_admit: end' in pg_get_functiondef(
              'public.admit_flow_engine_event_v1(jsonb,jsonb)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e528_native_payment_slip_missing';
    end if;
    if to_regprocedure('public._flow_engine_native_payment_slip_shape(jsonb)') is not null
       or position('e528_native_payment_slip' in pg_get_functiondef(
              'public._flow_engine_native_trigger(jsonb)'::regprocedure)) > 0 then
        raise exception using errcode = '55000', message = 'e528_native_payment_slip_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla: el ancla una sola vez, la marca ninguna; despues, la marca.
create function pg_temp.e528_native_payment_slip_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
    v_acl text;
begin
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'e528_native_payment_slip_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_e528_native_payment_slip_definition',
            detail = p_signature || ' ' || p_marker;
    end if;
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = p_signature::regprocedure;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e528_native_payment_slip_not_replaced',
            detail = p_signature || ' ' || p_marker;
    end if;
    -- create or replace conserva el duenio y los permisos: se verifica.
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from v_acl then
        raise exception using errcode = '55000', message = 'e528_native_payment_slip_acl_changed',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- 3 y 4. La espera anclada y la forma de la ficha (nuevas, inmutables).
-- ===========================================================================
create function public._flow_engine_native_payment_slip_due_day(p_wait jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog, pg_temp
as $function$
    -- e528_native_payment_slip: la espera de C3, el dia del vencimiento a las 09:00.
    select coalesce(p_wait = '{"vencimiento": {"dias_antes": 0, "a_las": "09:00"}}'::jsonb, false)
$function$;

create function public._flow_engine_native_payment_slip_shape(p_definition jsonb)
returns text
language plpgsql
immutable
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_step jsonb;
    v_sends integer := 0;
    v_anchored boolean := false;
begin
    -- e528_native_payment_slip: la forma de la ficha nativa (20261113200000).
    if jsonb_typeof(p_definition -> 'pasos') is distinct from 'array'
       or coalesce(jsonb_typeof(p_definition #> '{termina,si}') = 'array'
                   and p_definition #> '{termina,si}' @> '["contesta"]'::jsonb, false) then
        return 'motor_nativo:forma';
    end if;
    for v_step in select value from jsonb_array_elements(p_definition -> 'pasos') loop
        if jsonb_typeof(v_step) is distinct from 'object' then
            return 'motor_nativo:forma';
        end if;
        if v_step ? 'esperar' and (select count(*) from jsonb_object_keys(v_step)) = 1 then
            if public._flow_engine_native_payment_slip_due_day(v_step -> 'esperar') then
                if v_sends <> 1 or v_anchored then
                    return 'motor_nativo:forma';
                end if;
                v_anchored := true;
            elsif v_sends > 0 or public._flow_engine_duration_minutes(v_step -> 'esperar') is null then
                -- Solo la gracia antes de C1; despues de un envio, solo el ancla.
                return 'motor_nativo:forma';
            end if;
        elsif v_step ? 'plantilla' and not (v_step ? 'esperar' or v_step ? 'condicion'
                                            or v_step ? 'etiquetar' or v_step ? 'conversar'
                                            or v_step ? 'derivar') then
            if v_sends >= 2 or (v_sends = 1 and not v_anchored)
               or (v_step ? 'boton' and v_step -> 'boton' is distinct from '{"link": "ficha"}'::jsonb) then
                return 'motor_nativo:forma';
            end if;
            v_sends := v_sends + 1;
        else
            return 'motor_nativo:forma';
        end if;
    end loop;
    if v_sends = 0 or (v_anchored and v_sends < 2) then
        return 'motor_nativo:forma';
    end if;
    return null;
end;
$function$;

-- ===========================================================================
-- 8 (permiso). La audiencia de la ficha (nueva): el molde del pago rechazado.
-- ===========================================================================
create function public._flow_engine_native_payment_slip_audience(
    p_webhook_event_id uuid, p_contact_id uuid, p_offer text, p_destination text, p_tenant_ref text,
    out purchase_intent_id uuid, out precheckout_submission_id uuid, out reason text)
returns record
language plpgsql
stable
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_details public.commercial_ally_payment_slip_details%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_consent text;
    v_submission uuid;
begin
    -- e528_native_payment_slip: la intencion que la admision v2 correlaciono con ESTA ficha.
    purchase_intent_id := null;
    precheckout_submission_id := null;
    reason := 'no_entra:permiso';
    if not exists (
        select 1 from public.webhook_events event_row
         where event_row.id = p_webhook_event_id and event_row.source = 'hotmart'
           and event_row.event_type = 'PURCHASE_BILLET_PRINTED') then
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
      from public.commercial_ally_payment_slip_details details
     where details.webhook_event_id = p_webhook_event_id
       and details.trigger_kind = 'payment_slip'
       and details.correlation_outcome = 'resolved'
       and details.purchase_intent_id is not null;
    if not found then
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
-- 10. La lectura del contexto de la ficha nativa (nueva, service_role).
-- ===========================================================================
create function public.get_flow_engine_native_payment_slip_context_v1(
    p_action_id uuid, p_worker_id text, p_lease_generation bigint, p_now timestamptz)
returns table(webhook_event_id uuid, payment_type text, checkout_country_iso text, billet_url text,
              billet_barcode text, generated_at timestamptz)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
begin
    -- e528_native_payment_slip: la cerca de get_portable_payment_slip_send_context.
    if p_action_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_followup_fence';
    end if;
    select action.* into v_action from public.scheduled_actions action where action.id = p_action_id;
    if not found
       or not (
           v_action.lease_owner = p_worker_id
           and v_action.lease_generation = p_lease_generation
           and v_action.lease_expires_at > p_now
           and v_action.expires_at > p_now
           and v_action.status in ('pending', 'deferred', 'retryable_failed')
       ) then
        raise exception using errcode = '55000', message = 'current_action_lease_not_found';
    end if;
    if not exists (
        select 1 from public.flow_engine_native_runs native
         where native.recovery_case_id = v_action.recovery_case_id
           and native.trigger_event = 'ficha_impresa') then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_slip_required';
    end if;

    return query
    select details.webhook_event_id, details.payment_type, details.checkout_country_iso,
           details.billet_url, details.billet_barcode, details.generated_at
      from public.recovery_case_events case_event
      join public.commercial_ally_payment_slip_details details
        on details.webhook_event_id = case_event.webhook_event_id
     where case_event.recovery_case_id = v_action.recovery_case_id
       and case_event.event_role = 'payment_slip'
       and details.correlation_outcome = 'resolved'
     order by case_event.observed_at desc, case_event.created_at desc, details.webhook_event_id desc
     limit 1;
    if not found then
        raise exception using errcode = '55000', message = 'payment_slip_details_missing';
    end if;
end;
$function$;

-- ===========================================================================
-- 1. El disparador.
-- ===========================================================================
select pg_temp.e528_native_payment_slip_patch('public._flow_engine_native_trigger(jsonb)',
    E'        when p_definition -> ''dispara'' = ''{"evento": "intencion"}''::jsonb then ''intencion''\n',
    E'        when p_definition -> ''dispara'' = ''{"evento": "intencion"}''::jsonb then ''intencion''\n'
    || E'        -- e528_native_payment_slip_trigger: la ficha impresa (20261113200000).\n'
    || E'        when p_definition -> ''dispara'' = ''{"evento": "ficha_impresa"}''::jsonb then ''ficha_impresa''\n',
    'e528_native_payment_slip_trigger');

-- ===========================================================================
-- 2. La forma de los pasos: C1 y C3.
-- ===========================================================================
select pg_temp.e528_native_payment_slip_patch('public._flow_engine_native_steps(text)',
    E'        -- flow_engine_native_intent: el primer contacto del formulario, de un toque (20261101000100).\n',
    E'        -- e528_native_payment_slip_steps: la ficha, C1 y C3 (20261113200000).\n'
    || E'        when ''nativo:ficha_impresa'' then\n'
    || E'            ''[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto", "medio_de_pago", "vencimiento"], "variables_de": ["nombre", "producto", "medio_de_pago", "vencimiento"], "coupon_variables": null}, '
    || E'{"step_key": "native_touch_2", "coupon_ordinal": null, "variables": ["nombre", "medio_de_pago", "producto"], "variables_de": ["nombre", "medio_de_pago", "producto", "vencimiento"], "coupon_variables": null}]''::jsonb\n'
    || E'        -- flow_engine_native_intent: el primer contacto del formulario, de un toque (20261101000100).\n',
    'e528_native_payment_slip_steps');

-- ===========================================================================
-- 5. Lo que abre la base: la forma de la ficha.
-- ===========================================================================
select pg_temp.e528_native_payment_slip_patch('public._flow_engine_native_unsupported(jsonb)',
    E'        -- e483_native_personal_coupon: begin (20261112210500)\n',
    E'        -- e528_native_payment_slip_shape: begin (20261113200000)\n'
    || E'        -- La ficha: su forma propia, que cubre todo lo que sigue.\n'
    || E'        when public._flow_engine_native_trigger(p_definition) = ''ficha_impresa''\n'
    || E'            then public._flow_engine_native_payment_slip_shape(p_definition)\n'
    || E'        -- e528_native_payment_slip_shape: end\n'
    || E'        -- e483_native_personal_coupon: begin (20261112210500)\n',
    'e528_native_payment_slip_shape');

-- ===========================================================================
-- 6. El compilador: la espera anclada como un minuto.
-- ===========================================================================
select pg_temp.e528_native_payment_slip_patch(
    'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)',
    E'            v_minutes := public._flow_engine_duration_minutes(v_step -> ''esperar'');\n'
    || E'            if v_minutes is null then\n',
    E'            v_minutes := public._flow_engine_duration_minutes(v_step -> ''esperar'');\n'
    || E'            -- e528_native_payment_slip_wait: la espera anclada de C3 despues de C1 es el\n'
    || E'            -- minuto del legado; el dia y la hora los pone el bridge (20261113200000).\n'
    || E'            if v_minutes is null and p_legacy_kind = ''nativo:ficha_impresa'' and v_sends = 1\n'
    || E'               and public._flow_engine_native_payment_slip_due_day(v_step -> ''esperar'') then\n'
    || E'                v_minutes := 1;\n'
    || E'            end if;\n'
    || E'            if v_minutes is null then\n',
    'e528_native_payment_slip_wait');

-- ===========================================================================
-- 7. La admision: el evento de la ficha.
-- ===========================================================================
select pg_temp.e528_native_payment_slip_patch('public.admit_flow_engine_event_v1(jsonb,jsonb)',
    E'       or coalesce(p_flow_event ->> ''tipo'', '''') not in (''compra'', ''carrito'', ''pago_fallido'')\n',
    E'       -- e528_native_payment_slip_type: y la ficha impresa (20261113200000).\n'
    || E'       or coalesce(p_flow_event ->> ''tipo'', '''') not in (''compra'', ''carrito'', ''pago_fallido'', ''ficha_impresa'')\n',
    'e528_native_payment_slip_type');
select pg_temp.e528_native_payment_slip_patch('public.admit_flow_engine_event_v1(jsonb,jsonb)',
    E'                                                    when ''pago_fallido'' then ''PURCHASE_CANCELED''\n',
    E'                                                    when ''pago_fallido'' then ''PURCHASE_CANCELED''\n'
    || E'                                                    -- e528_native_payment_slip_event\n'
    || E'                                                    when ''ficha_impresa'' then ''PURCHASE_BILLET_PRINTED''\n',
    'e528_native_payment_slip_event');

-- ===========================================================================
-- 8. La apertura del recorrido.
-- ===========================================================================
-- El doble caso: una ficha del legado abierta gana.
select pg_temp.e528_native_payment_slip_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'    -- flow_engine_native_intent_double_case: begin\n',
    E'    -- e528_native_payment_slip_double_case: begin (20261113200000)\n'
    || E'    -- Una ficha del legado abierta de la persona y del producto, de cualquier\n'
    || E'    -- oferta, que no es nativa: la ficha es del legado (el bridge: sin_reclamo).\n'
    || E'    if public._flow_engine_native_trigger(v_definition.definition) = ''ficha_impresa''\n'
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
    || E'              and legacy_case.context ->> ''trigger_kind'' = ''payment_slip''\n'
    || E'              and legacy_case.status in (''grace_period'', ''active'', ''paused'')\n'
    || E'              and not exists (\n'
    || E'                  select 1 from public.flow_engine_native_runs native_case\n'
    || E'                   where native_case.recovery_case_id = legacy_case.id)) then\n'
    || E'        perform public._flow_engine_record_start_rejection(\n'
    || E'            p_control.flow_id, v_definition.version, p_event.id, ''admision'', ''caso_del_legado_en_curso'');\n'
    || E'        return query select ''motor_no_arranca''::text, ''caso_del_legado_en_curso''::text,\n'
    || E'            null::uuid, null::uuid, null::uuid, null::boolean;\n'
    || E'        return;\n'
    || E'    end if;\n'
    || E'    -- e528_native_payment_slip_double_case: end\n'
    || E'\n'
    || E'    -- flow_engine_native_intent_double_case: begin\n',
    'e528_native_payment_slip_double_case');
-- El permiso.
select pg_temp.e528_native_payment_slip_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'    -- flow_engine_native_intent_permission: el formulario (20261101000100), con\n',
    E'    -- e528_native_payment_slip_permission: begin (20261113200000)\n'
    || E'    -- La ficha, con la intencion que la admision v2 correlaciono con ESTA ficha.\n'
    || E'    elsif public._flow_engine_native_trigger(v_definition.definition) = ''ficha_impresa''\n'
    || E'       and p_event.event_type = ''PURCHASE_BILLET_PRINTED'' then\n'
    || E'        select audience.purchase_intent_id, audience.precheckout_submission_id, audience.reason\n'
    || E'          into v_audience_intent, v_audience_submission, v_reason\n'
    || E'          from public._flow_engine_native_payment_slip_audience(\n'
    || E'              p_event.id, p_contact_id, p_offer, p_destination, p_control.tenant_ref) audience;\n'
    || E'    -- e528_native_payment_slip_permission: end\n'
    || E'    -- flow_engine_native_intent_permission: el formulario (20261101000100), con\n',
    'e528_native_payment_slip_permission');
-- La segunda ficha se suma al caso en curso.
select pg_temp.e528_native_payment_slip_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'    -- flow_engine_native_payment_failure_last_refusal: end\n',
    E'    -- flow_engine_native_payment_failure_last_refusal: end\n'
    || E'    -- e528_native_payment_slip_repeat: begin (20261113200000)\n'
    || E'    -- Una segunda ficha con el recorrido en curso de la persona (y oferta) no abre\n'
    || E'    -- otro: se suma al caso como evento de la ficha, sin mover ningun envio. C1 y\n'
    || E'    -- C3 usan la mas reciente (get_flow_engine_native_payment_slip_context_v1).\n'
    || E'    if v_reason = ''ya_en_curso''\n'
    || E'       and public._flow_engine_native_trigger(v_definition.definition) = ''ficha_impresa'' then\n'
    || E'        insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)\n'
    || E'        select run.recovery_case_id, p_event.id, ''payment_slip'', p_moment\n'
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
    || E'    end if;\n'
    || E'    -- e528_native_payment_slip_repeat: end\n',
    'e528_native_payment_slip_repeat');
-- La secuencia es de una ficha.
select pg_temp.e528_native_payment_slip_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'    values (v_case_id, ''active'', ''recovery'', v_policy_key, v_definition.version, 0, v_stored.max_automatic_messages)\n',
    E'    values (v_case_id, ''active'',\n'
    || E'            -- e528_native_payment_slip_sequence: la secuencia de la ficha es de una ficha (20261113200000).\n'
    || E'            case when public._flow_engine_native_trigger(v_definition.definition) = ''ficha_impresa''\n'
    || E'                 then ''payment_slip'' else ''recovery'' end,\n'
    || E'            v_policy_key, v_definition.version, 0, v_stored.max_automatic_messages)\n',
    'e528_native_payment_slip_sequence');
-- La ficha como evento del caso.
select pg_temp.e528_native_payment_slip_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'        values (v_case_id, p_event.id, ''precheckout_intent'', p_moment);\n'
    || E'    end if;\n',
    E'        values (v_case_id, p_event.id, ''precheckout_intent'', p_moment);\n'
    || E'    end if;\n'
    || E'    -- e528_native_payment_slip_case_event: la ficha que abre el caso (20261113200000).\n'
    || E'    if public._flow_engine_native_trigger(v_definition.definition) = ''ficha_impresa'' then\n'
    || E'        insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)\n'
    || E'        values (v_case_id, p_event.id, ''payment_slip'', p_moment);\n'
    || E'    end if;\n',
    'e528_native_payment_slip_case_event');

-- ===========================================================================
-- 9. La puerta antes de cada envio: la intencion de la ficha.
-- ===========================================================================
select pg_temp.e528_native_payment_slip_patch(
    'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)',
    E'    if v_native.trigger_event in (''carrito'', ''pago_fallido'') and not v_native.audience_hotmart_phone then\n',
    E'    -- e528_native_payment_slip_recheck: y la ficha (20261113200000).\n'
    || E'    if v_native.trigger_event in (''carrito'', ''pago_fallido'', ''ficha_impresa'') and not v_native.audience_hotmart_phone then\n',
    'e528_native_payment_slip_recheck');

-- ===========================================================================
-- 12. Las lecturas: la simulacion, la historia del AE y los resultados (E-531).
-- ===========================================================================
select pg_temp.e528_native_payment_slip_patch(
    'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)',
    E'    if p_trigger is null or p_trigger not in (''compra'', ''carrito'', ''pago_fallido'', ''intencion'') then\n',
    E'    -- e528_native_payment_slip_counts_trigger: y la ficha impresa (20261113200000).\n'
    || E'    if p_trigger is null or p_trigger not in (''compra'', ''carrito'', ''pago_fallido'', ''intencion'', ''ficha_impresa'') then\n',
    'e528_native_payment_slip_counts_trigger');
select pg_temp.e528_native_payment_slip_patch(
    'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)',
    E'                                         when ''pago_fallido'' then ''PURCHASE_CANCELED''\n',
    E'                                         when ''pago_fallido'' then ''PURCHASE_CANCELED''\n'
    || E'                                         -- e528_native_payment_slip_counts_kind: la ficha trae checkout_phone.\n'
    || E'                                         when ''ficha_impresa'' then ''PURCHASE_BILLET_PRINTED''\n',
    'e528_native_payment_slip_counts_kind');
select pg_temp.e528_native_payment_slip_patch(
    'public.read_automation_expert_native_history_v1(jsonb,uuid)',
    E'                                    when ''intencion'' then ''PRECHECKOUT_FORM_SUBMITTED'' end)\n',
    E'                                    when ''intencion'' then ''PRECHECKOUT_FORM_SUBMITTED''\n'
    || E'                                    -- e528_native_payment_slip_history (20261113200000)\n'
    || E'                                    when ''ficha_impresa'' then ''PURCHASE_BILLET_PRINTED'' end)\n',
    'e528_native_payment_slip_history');
select pg_temp.e528_native_payment_slip_patch(
    'public.read_results_v1(timestamptz,timestamptz,text,text[],integer,integer)',
    E'                        case c.trigger_event when ''compra'' then ''bienvenida'' when ''carrito'' then ''carrito''\n',
    E'                        -- e528_native_payment_slip_results: la ficha nativa es ficha_oxxo, como la del legado.\n'
    || E'                        case c.trigger_event when ''compra'' then ''bienvenida'' when ''carrito'' then ''carrito''\n'
    || E'                             when ''ficha_impresa'' then ''ficha_oxxo''\n',
    'e528_native_payment_slip_results');

-- ===========================================================================
-- Los permisos: los helpers no son de la API; la lectura, solo service_role.
-- ===========================================================================
do $acl$
declare
    v_signature text;
    v_role text;
begin
    foreach v_signature in array array[
        'public._flow_engine_native_payment_slip_due_day(jsonb)',
        'public._flow_engine_native_payment_slip_shape(jsonb)',
        'public._flow_engine_native_payment_slip_audience(uuid,uuid,text,text,text)',
        'public.get_flow_engine_native_payment_slip_context_v1(uuid,text,bigint,timestamptz)'
    ] loop
        execute format('revoke all on function %s from public', v_signature);
        foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
            if exists (select 1 from pg_roles where rolname = v_role) then
                execute format('revoke all on function %s from %I', v_signature, v_role);
            end if;
        end loop;
    end loop;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.get_flow_engine_native_payment_slip_context_v1(uuid,text,bigint,timestamptz)
            to service_role;
    end if;
end;
$acl$;

do $check$
begin
    if public._flow_engine_native_trigger('{"dispara": {"evento": "ficha_impresa"}}'::jsonb) is distinct from 'ficha_impresa'
       or public._flow_engine_native_trigger('{"dispara": {"evento": "ficha_vencida"}}'::jsonb) is not null
       or jsonb_array_length(public._flow_engine_native_steps('nativo:ficha_impresa')) <> 2
       or jsonb_array_length(public._flow_engine_native_steps('nativo:compra')) <> 1
       or public._flow_engine_native_payment_slip_due_day('{"vencimiento": {"a_las": "09:00", "dias_antes": 0}}'::jsonb)
          is not true
       or public._flow_engine_native_payment_slip_due_day('"1m"'::jsonb) is not false
       or position('e528_native_payment_slip_case_event' in pg_get_functiondef(
              'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e528_native_payment_slip_not_applied';
    end if;
end;
$check$;

-- ===========================================================================
-- 11. La fila del recorrido: el disparador y la intencion de la ficha. Al final:
-- ACCESS EXCLUSIVE lo menos posible.
-- ===========================================================================
alter table public.flow_engine_native_runs
    drop constraint flow_engine_native_runs_trigger_event_check,
    add constraint flow_engine_native_runs_trigger_event_check
        -- e483_native_audience: y la audiencia (20261112210600).
        -- e528_native_payment_slip: y la ficha impresa (20261113200000).
        check (trigger_event in ('compra', 'carrito', 'pago_fallido', 'intencion', 'audiencia', 'ficha_impresa')),
    drop constraint flow_engine_native_runs_audience_by_trigger,
    add constraint flow_engine_native_runs_audience_by_trigger check (
        -- e483_payment_failure_by_phone: el pago rechazado de una oferta de
        -- campana, con el telefono de Hotmart y sin intencion (20261112210400).
        (audience_hotmart_phone
         and trigger_event = 'pago_fallido'
         and audience_purchase_intent_id is null and audience_precheckout_submission_id is null)
        -- e528_native_payment_slip: la ficha lleva su intencion, como el carrito (20261113200000).
        or (not audience_hotmart_phone
            and trigger_event in ('carrito', 'pago_fallido', 'intencion', 'ficha_impresa')
            and audience_purchase_intent_id is not null and audience_precheckout_submission_id is not null)
        or (not audience_hotmart_phone
            and trigger_event not in ('carrito', 'pago_fallido', 'intencion', 'ficha_impresa')
            and audience_purchase_intent_id is null and audience_precheckout_submission_id is null));

commit;
