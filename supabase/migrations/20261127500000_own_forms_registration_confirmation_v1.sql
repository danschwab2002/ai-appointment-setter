-- own-forms-registration-confirmation-v1: la confirmacion de registro a un
-- evento por el motor nativo (4.3, segunda parte; la primera es el PR #525,
-- own-forms-v1 «Registro a un evento», D-43.1 a D-43.5).
--
-- Por que. Desde #525 un formulario propio con tipo = "registro" se admite sin
-- planificar el primer contacto comercial, y un flujo puede decir
-- dispara: {evento: intencion, registro: <clave del evento>}, pero ninguna base
-- sabia rutear ese envio a ese flujo: la regla 14 lo marcaba «todavia no» y no
-- salia nada. Esta migracion es la pieza de la base que faltaba.
--
-- Que cambia, por bloques:
--
-- 1. _flow_engine_native_trigger reconoce la forma con registro como
--    intencion (D-43.6): {evento: intencion, registro: <clave>} y nada mas, con
--    la clave como la escribe el esquema (a-z, 0-9, guiones; 3 a 40). Asi el
--    caso, el permiso (el envio consentido que lo disparo), la reevaluacion,
--    los frenos del legado, el arranque con los locks de opt-out y la forma de
--    los pasos (nativo:intencion) son los del primer contacto nativo de hoy,
--    sin una sola rama nueva.
-- 2. _flow_engine_native_unsupported: un flujo de registro manda un solo
--    mensaje (D-43.7): con mas de una plantilla, motor_nativo:forma. Es lo
--    mismo que dice la capacidad intencion:registro del bridge.
-- 3. admit_and_route_portable_lead_precheckout_v1 (el formulario comercial):
--    la seleccion de los flujos de intencion excluye los que tienen registro, y
--    el toque nativo de 24 h no cuenta un caso de registro (D-43.8). Asi un
--    envio comercial nunca lo toma un flujo de registro y una confirmacion de
--    registro no le quita el primer contacto comercial a nadie.
-- 4. _flow_engine_admit_native_flow, con un flujo de registro: el caso lleva
--    context.registration_event (la clave del evento); el doble caso con el
--    legado no lo frena (una confirmacion no compite con el primer contacto
--    comercial); y la repeticion de D21 separa (D-43.8): el comercial mira solo
--    recorridos sin registration_event y el de registro solo los del mismo
--    evento (una confirmacion por persona y evento).
-- 5. admit_and_route_registration_lead_precheckout_v1 (nueva, la entrada del
--    registro): admite el envio con admit_portable_observed_lead_precheckout
--    (lo de siempre) y lo prueba SOLO en los flujos nativos del producto cuya
--    version activa dispara con ese registro; nunca el legado. Sin un flujo
--    asi: solo la admision (ruta sin_flujo), que es lo que hace hoy el bridge.
--    Con p_health_brake (el freno por salud de Meta que lee el bridge, D-43.9)
--    no abre ningun caso: cada flujo deja motor_no_arranca con ese motivo y su
--    rechazo (MOT-002 lo avisa). El resultado por envio y por flujo va a
--    flow_engine_native_intent_admissions (ruta nativo), como el comercial.
-- 6. Los permisos: la entrada nueva, solo service_role.
--
-- Lo que no cambia. Un flujo de intencion sin registro se comporta byte a byte
-- como hoy: todas las ramas nuevas preguntan por la clave registro, que hoy
-- no existe en ninguna definicion publicada (la regla 14 no la dejaba
-- publicar). El bridge llama a la entrada nueva solo con
-- OWN_FORMS_REGISTRATION_FLOW_ENABLED, OWN_FORMS_ENABLED, FLOW_ENGINE_ENABLED y
-- FLOW_ENGINE_NATIVE_INTENT_ENABLED prendidos.
--
-- Lo que no hace: no crea tablas ni roles, no cambia el ACL de lo que ya
-- existe (create or replace y los reemplazos lo conservan), no toca filas.
-- Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.admit_and_route_portable_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,integer)') is null
       or to_regprocedure('public._flow_engine_native_intent_audience(uuid,uuid,uuid,text,text,text)') is null
       or to_regprocedure('public.admit_portable_observed_lead_precheckout(text,text,integer,text,jsonb,jsonb)') is null
       or to_regprocedure('public._flow_engine_record_start_rejection(uuid,integer,uuid,text,text)') is null
       or to_regclass('public.flow_engine_native_intent_admissions') is null then
        raise exception using errcode = '55000', message = 'own_forms_registration_confirmation_missing';
    end if;
    if to_regprocedure('public.admit_and_route_registration_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,text)') is not null then
        raise exception using errcode = '55000', message = 'own_forms_registration_confirmation_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla, con la misma verificacion para todos: la funcion
-- existe, es del dueno que corre la migracion, el ancla aparece una sola vez y
-- el marcador todavia no esta.
create function pg_temp.own_forms_registration_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'own_forms_registration_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'own_forms_registration_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_own_forms_registration_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'own_forms_registration_definition_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- Bloques 1 a 4. Los reemplazos sobre la definicion vigente.
-- ===========================================================================

do $patch$
declare
    c_trigger constant text := 'public._flow_engine_native_trigger(jsonb)';
    c_unsupported constant text := 'public._flow_engine_native_unsupported(jsonb)';
    c_route constant text :=
        'public.admit_and_route_portable_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,integer)';
    c_admit constant text :=
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)';
begin
    -- 1. El disparador: la forma con registro es intencion.
    perform pg_temp.own_forms_registration_patch(c_trigger,
        E'        when p_definition -> ''dispara'' = ''{"evento": "intencion"}''::jsonb then ''intencion''\n',
        E'        when p_definition -> ''dispara'' = ''{"evento": "intencion"}''::jsonb then ''intencion''\n'
        || E'        -- own_forms_registration_trigger: el registro a un evento, {evento: intencion,\n'
        || E'        -- registro: <clave>} y nada mas, con la clave del esquema (20261127500000).\n'
        || E'        when jsonb_typeof(p_definition -> ''dispara'') = ''object''\n'
        || E'             and (p_definition -> ''dispara'') - ''registro'' = ''{"evento": "intencion"}''::jsonb\n'
        || E'             and jsonb_typeof(p_definition #> ''{dispara,registro}'') = ''string''\n'
        || E'             and p_definition #>> ''{dispara,registro}'' ~ ''^[a-z0-9][a-z0-9-]{2,39}$'' then ''intencion''\n',
        'own_forms_registration_trigger');

    -- 2. Un flujo de registro manda un solo mensaje.
    perform pg_temp.own_forms_registration_patch(c_unsupported,
        E'        -- e483_native_multitouch: begin (20261112210000)\n',
        E'        -- own_forms_registration_single_send: un flujo de registro es una sola\n'
        || E'        -- confirmacion (20261127500000, D-43.7): la capacidad intencion:registro.\n'
        || E'        when public._flow_engine_native_trigger(p_definition) = ''intencion''\n'
        || E'             and p_definition -> ''dispara'' ? ''registro''\n'
        || E'             and (select count(*) from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array''\n'
        || E'                                                     then p_definition -> ''pasos'' else ''[]''::jsonb end) step(value)\n'
        || E'                  where jsonb_typeof(step.value) = ''object'' and step.value ? ''plantilla'') > 1\n'
        || E'            then ''motor_nativo:forma''\n'
        || E'        -- e483_native_multitouch: begin (20261112210000)\n',
        'own_forms_registration_single_send');

    -- 3a. El formulario comercial no toma un flujo de registro.
    perform pg_temp.own_forms_registration_patch(c_route,
        E'           and d.definition #>> ''{dispara,evento}'' = ''intencion'';\n',
        E'           and d.definition #>> ''{dispara,evento}'' = ''intencion''\n'
        || E'           -- own_forms_registration_route_excludes: un flujo de registro no toma un\n'
        || E'           -- envio comercial (20261127500000).\n'
        || E'           and not coalesce(d.definition -> ''dispara'' ? ''registro'', false);\n',
        'own_forms_registration_route_excludes');
    -- 3b. El toque nativo de 24 h del formulario comercial no cuenta un registro.
    perform pg_temp.own_forms_registration_patch(c_route,
        E'                     where native.trigger_event = ''intencion'' and rc.contact_id = v_found_contact\n',
        E'                     where native.trigger_event = ''intencion'' and rc.contact_id = v_found_contact\n'
        || E'                       -- own_forms_registration_route_touch: una confirmacion de registro no\n'
        || E'                       -- es el primer contacto comercial (20261127500000).\n'
        || E'                       and rc.context ->> ''registration_event'' is null\n',
        'own_forms_registration_route_touch');

    -- 4a. El doble caso con el legado no frena una confirmacion de registro.
    perform pg_temp.own_forms_registration_patch(c_admit,
        E'    if public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
        || E'       and (exists (\n',
        E'    if public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
        || E'       -- own_forms_registration_double_case: una confirmacion de registro no\n'
        || E'       -- compite con el primer contacto comercial del legado (20261127500000).\n'
        || E'       and not coalesce(v_definition.definition -> ''dispara'' ? ''registro'', false)\n'
        || E'       and (exists (\n',
        'own_forms_registration_double_case');
    -- 4b. La repeticion de D21: el comercial con los comerciales, el registro con su evento.
    perform pg_temp.own_forms_registration_patch(c_admit,
        E'         where native.trigger_event = ''intencion'' and rc.contact_id = p_contact_id\n',
        E'         where native.trigger_event = ''intencion'' and rc.contact_id = p_contact_id\n'
        || E'           -- own_forms_registration_repeat: un recorrido comercial no frena una\n'
        || E'           -- confirmacion ni al reves; dos registros al mismo evento, si (20261127500000).\n'
        || E'           and (rc.context ->> ''registration_event'')\n'
        || E'               is not distinct from (v_definition.definition #>> ''{dispara,registro}'')\n',
        'own_forms_registration_repeat');
    -- 4c. El caso lleva la clave del evento.
    perform pg_temp.own_forms_registration_patch(c_admit,
        E'                                        ''precheckout_submitted_at'', p_moment)\n'
        || E'                else ''{}''::jsonb end\n',
        E'                                        ''precheckout_submitted_at'', p_moment)\n'
        || E'                else ''{}''::jsonb end\n'
        || E'        -- own_forms_registration_context: la clave del evento de un registro (20261127500000).\n'
        || E'        || case when public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
        || E'                     and v_definition.definition -> ''dispara'' ? ''registro''\n'
        || E'                then jsonb_build_object(''registration_event'',\n'
        || E'                                        v_definition.definition #>> ''{dispara,registro}'')\n'
        || E'                else ''{}''::jsonb end\n',
        'own_forms_registration_context');
end;
$patch$;

-- ===========================================================================
-- Bloque 5. La entrada del registro.
-- ===========================================================================

create function public.admit_and_route_registration_lead_precheckout_v1(
    p_tenant_ref text, p_funnel_ref text, p_binding_version integer, p_external_submission_id text,
    p_raw_payload jsonb, p_canonical_payload jsonb, p_registration_event text, p_health_brake text)
returns table(outcome text, submission_id uuid, purchase_intent_id uuid, plan_outcome text, plan_reason text,
              ruta text, flujos jsonb)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_flow_ids uuid[] := array[]::uuid[];
    v_flow_id uuid;
    v_claimers uuid[] := array[]::uuid[];
    v_any_on boolean := false;
    v_control public.flow_controls%rowtype;
    v_definition jsonb;
    v_vence integer;
    v_admission_outcome text;
    v_submission_id uuid;
    v_intent_id uuid;
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_submitted_at timestamptz;
    v_found_contact uuid;
    v_find_reason text;
    v_contact_id uuid;
    v_destination text;
    v_event public.webhook_events%rowtype;
    v_one record;
    v_reason text;
    v_rows jsonb := '[]'::jsonb;
    v_sqlstate text;
    v_message text;
    v_detail text;
begin
    -- D-43.6: la clave como la escribe el esquema (flow_definition._REGISTRATION_EVENT).
    if p_registration_event is null or p_registration_event !~ '^[a-z0-9][a-z0-9-]{2,39}$' then
        raise exception using errcode = '22023', message = 'invalid_registration_event';
    end if;
    -- D-43.9: el freno lo decide el bridge; aca solo su forma (meta_salud:<que>).
    if p_health_brake is not null and p_health_brake !~ '^meta_salud:[a-z_]{1,40}$' then
        raise exception using errcode = '22023', message = 'invalid_health_brake';
    end if;

    -- Los flujos nativos del producto cuya version activa dispara con este
    -- registro, exactamente: ruta nativa, no apagados.
    select binding.* into v_binding
      from public.commercial_ally_runtime_bindings binding
     where binding.tenant_ref = p_tenant_ref and binding.funnel_ref = p_funnel_ref
       and binding.binding_version = p_binding_version and binding.status = 'active';
    if found then
        select coalesce(array_agg(c.flow_id order by c.flow_id), array[]::uuid[]) into v_flow_ids
          from public.flow_controls c
          join public.flow_engine_routes r on r.flow_id = c.flow_id and r.legacy_kind is null
          join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
         where c.tenant_ref = v_binding.tenant_ref and c.ally_ref = v_binding.ally_ref
           and c.product_ref = v_binding.funnel_ref and c.state <> 'apagado'
           and d.definition -> 'dispara'
               = jsonb_build_object('evento', 'intencion', 'registro', p_registration_event);
    end if;

    -- (a) El orden del comercial: los flujos (FOR SHARE, en orden de flow_id),
    -- despues la intencion y el contacto (adentro de la admision).
    perform 1 from public.flow_engine_routes r where r.flow_id = any(v_flow_ids) order by r.flow_id for share;
    perform 1 from public.flow_controls c where c.flow_id = any(v_flow_ids) order by c.flow_id for share;

    -- (b) La admision de siempre: el envio queda guardado pase lo que pase con
    -- los flujos (lo que lee la historia de la persona).
    select admission.outcome, admission.submission_id, admission.purchase_intent_id
      into strict v_admission_outcome, v_submission_id, v_intent_id
      from public.admit_portable_observed_lead_precheckout(
          p_tenant_ref, p_funnel_ref, p_binding_version, p_external_submission_id,
          p_raw_payload, p_canonical_payload) admission;

    if v_admission_outcome <> 'inserted' then
        -- Un reenvio: lo que se decidio para ese envio.
        select coalesce(jsonb_agg(jsonb_build_object(
                   'flow_id', x.flow_id, 'version', x.version, 'outcome', x.outcome,
                   'reason', x.reason, 'recovery_case_id', x.recovery_case_id) order by x.flow_id),
               '[]'::jsonb)
          into v_rows
          from public.flow_engine_native_intent_admissions x
         where x.submission_id = v_submission_id and x.ruta = 'nativo';
        return query select v_admission_outcome, v_submission_id, v_intent_id, null::text, null::text,
            case when jsonb_array_length(v_rows) > 0 then 'nativo' else 'sin_flujo' end, v_rows;
        return;
    end if;
    if cardinality(v_flow_ids) = 0 then
        -- Ningun flujo confirma este registro: solo la admision, lo de hoy.
        return query select v_admission_outcome, v_submission_id, v_intent_id, null::text, null::text,
            'sin_flujo'::text, '[]'::jsonb;
        return;
    end if;

    -- (c) a (g): el nativo en una subtransaccion. Una falla que no es
    -- transitoria se deshace sola y queda como error_interno de cada flujo: la
    -- admision no se pierde (no hay legado al que devolverle el envio).
    begin
        select intent.* into strict v_intent from public.purchase_intents intent where intent.id = v_intent_id;
        select submission.* into strict v_submission
          from public.precheckout_submissions submission where submission.id = v_submission_id;
        v_submitted_at := (v_submission.canonical_payload #>> '{submitted_at}')::timestamptz;

        -- El contacto, sin crearlo: decide la cohorte.
        select found.contact_id, found.reason_code into v_found_contact, v_find_reason
          from public._find_portable_precheckout_contact(v_intent_id) found;

        -- Quien reclama: prendido siempre; cohorte solo con el contacto adentro.
        foreach v_flow_id in array v_flow_ids loop
            select c.* into strict v_control from public.flow_controls c where c.flow_id = v_flow_id;
            if v_control.state = 'pausado' then
                perform public._flow_engine_record_start_rejection(
                    v_flow_id, v_control.active_version, v_submission_id, 'admision', 'flujo_pausado');
                v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
                    'version', v_control.active_version, 'outcome', 'motor_no_arranca',
                    'reason', 'flujo_pausado', 'recovery_case_id', null));
            elsif v_control.state = 'cohorte' and (v_found_contact is null or not exists (
                select 1 from public.flow_cohort_members member
                 where member.flow_id = v_flow_id and member.contact_id = v_found_contact)) then
                v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
                    'version', v_control.active_version, 'outcome', 'motor_no_arranca',
                    'reason', 'fuera_de_cohorte', 'recovery_case_id', null));
            else
                v_claimers := v_claimers || v_flow_id;
                v_any_on := v_any_on or v_control.state = 'prendido';
            end if;
        end loop;

        if cardinality(v_claimers) > 0 then
            -- (c') El freno por salud de Meta (D-43.9), antes de crear nada: ni
            -- contacto, ni ancla, ni caso.
            v_reason := p_health_brake;

            -- (c) Lo que se decide sin contacto: la intencion viva y autorizada,
            -- el consentimiento del envio y los frenos del legado sin contacto
            -- (el mismo orden y los mismos motivos que el formulario comercial).
            if v_reason is null then
                v_reason := case
                    when v_intent.lifecycle_state = 'purchased' then 'intent_purchased'
                    when v_intent.lifecycle_state <> 'waiting_for_purchase'
                         or v_intent.provisional
                         or not v_intent.provider_observed
                         or v_intent.normalized_phone is null
                         or coalesce(v_intent.current_classification, '') in (
                             'identity_conflict', 'tracking_incomplete', 'expired_unknown')
                        then 'precheckout_intent_not_live'
                    when not v_intent.whatsapp_contact_authorized or not v_intent.activation_authorized
                        then 'precheckout_intent_not_authorized'
                    when v_submission.contract_version <> '1.1.0'
                         or not v_submission.activation_authorized
                         or v_submission.canonical_payload #>> '{consent,whatsapp_contact}' is distinct from 'true'
                         or v_submission.canonical_payload #>> '{consent,marketing_optin}' is distinct from 'true'
                        then 'precheckout_submission_not_consented'
                    when v_submitted_at is null then 'precheckout_submission_expired'
                end;
            end if;
            if v_reason is null then
                v_reason := public._portable_precheckout_stop_reason(v_intent_id, null);
            end if;

            -- (d) El contacto: creado solo si algun flujo esta prendido (en
            -- cohorte no se crean contactos). Con el contacto, los frenos otra vez.
            if v_reason is null then
                if v_any_on then
                    begin
                        v_contact_id := public._ensure_portable_precheckout_contact(v_intent_id, v_submission_id);
                    exception when sqlstate '55000' then
                        get stacked diagnostics v_message = message_text, v_detail = pg_exception_detail;
                        if v_message = 'pilot_scope_rejected' and v_detail ~ '^[a-z0-9_]{1,64}$' then
                            v_reason := v_detail;
                        else
                            raise;
                        end if;
                    end;
                elsif v_find_reason = 'precheckout_contact_found' then
                    v_contact_id := v_found_contact;
                else
                    v_reason := coalesce(v_find_reason, 'precheckout_contact_unresolved');
                end if;
            end if;
            if v_reason is null then
                v_reason := public._portable_precheckout_stop_reason(v_intent_id, v_contact_id);
            end if;

            if v_reason is not null then
                foreach v_flow_id in array v_claimers loop
                    select c.* into strict v_control from public.flow_controls c where c.flow_id = v_flow_id;
                    perform public._flow_engine_record_start_rejection(
                        v_flow_id, v_control.active_version, v_submission_id, 'admision', v_reason);
                    v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
                        'version', v_control.active_version, 'outcome', 'motor_no_arranca',
                        'reason', public._flow_engine_reason(v_reason, 'motivo_invalido'),
                        'recovery_case_id', null));
                end loop;
            else
                -- (e) El destino, como el formulario comercial: la identidad de
                -- WhatsApp que ya exista del contacto, primero la activa y la textual.
                select identity_row.external_user_id into v_destination
                  from public.channel_identities identity_row
                 where identity_row.channel = 'whatsapp'
                   and identity_row.account_id = 'chatwoot:' || v_binding.chatwoot_account_id::text
                   and identity_row.external_user_id = any(public._whatsapp_phone_variants(v_intent.normalized_phone))
                   and identity_row.contact_id = v_contact_id
                 order by (identity_row.identity_status = 'active') desc,
                          (identity_row.external_user_id = v_intent.normalized_phone) desc,
                          identity_row.external_user_id
                 limit 1;
                v_destination := coalesce(v_destination, v_intent.normalized_phone);

                -- (f) El ancla: un webhook_events de fuente system por envio, con
                -- la forma del formulario (el permiso de _flow_engine_admit_native_flow
                -- la lee) y un id propio del registro.
                insert into public.webhook_events (
                    source, external_event_id, event_type, payload, processing_status, processed_at)
                values (
                    'system', 'flow-engine-registration:' || v_submission_id::text, 'PRECHECKOUT_FORM_SUBMITTED',
                    jsonb_build_object('purchase_intent_id', v_intent_id,
                                       'precheckout_submission_id', v_submission_id,
                                       'creation_date', floor(extract(epoch from v_submitted_at) * 1000)::bigint),
                    'processed', clock_timestamp())
                returning * into v_event;

                -- (g) Cada flujo en su subtransaccion. Un error transitorio se relanza.
                foreach v_flow_id in array v_claimers loop
                    select c.* into strict v_control from public.flow_controls c where c.flow_id = v_flow_id;
                    select d.definition into strict v_definition
                      from public.flow_definitions d
                     where d.flow_id = v_flow_id and d.version = v_control.active_version;
                    v_vence := public._flow_engine_duration_minutes(v_definition #> '{termina,vence}');
                    if v_vence is not null
                       and v_submitted_at + v_vence * interval '1 minute' <= clock_timestamp() then
                        perform public._flow_engine_record_start_rejection(
                            v_flow_id, v_control.active_version, v_event.id, 'admision',
                            'precheckout_submission_expired');
                        select 'motor_no_arranca'::text as outcome, 'precheckout_submission_expired'::text as reason,
                               null::uuid as recovery_case_id
                          into v_one;
                    else
                        begin
                            select one.outcome, one.reason, one.recovery_case_id into v_one
                              from public._flow_engine_admit_native_flow(
                                  v_control, v_event, v_contact_id, v_intent.offer_ref, v_submitted_at,
                                  v_binding.chatwoot_account_id, v_binding.chatwoot_inbox_id,
                                  v_destination) one;
                            set constraints all immediate;
                        exception when others then
                            get stacked diagnostics v_sqlstate = returned_sqlstate, v_message = message_text;
                            if v_sqlstate like '40%' or v_sqlstate like '53%' or v_sqlstate like '57%'
                               or v_sqlstate like '08%' or v_sqlstate = '55P03' then
                                raise;
                            end if;
                            -- La identidad de otro contacto, con nombre (como 20261125000100).
                            v_reason := case when v_sqlstate = '23514' and v_message = 'channel_identity_contact_mismatch'
                                             then 'no_entra:identidad_de_otro_contacto'
                                             else 'error_interno:' || lower(v_sqlstate) end;
                            perform public._flow_engine_record_start_rejection(
                                v_flow_id, v_control.active_version, v_event.id, 'admision', v_reason);
                            select 'motor_no_arranca'::text as outcome, v_reason as reason,
                                   null::uuid as recovery_case_id
                              into v_one;
                        end;
                        set constraints all deferred;
                    end if;
                    v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
                        'version', v_control.active_version, 'outcome', v_one.outcome, 'reason', v_one.reason,
                        'recovery_case_id', v_one.recovery_case_id));
                end loop;
            end if;
        end if;
    exception when others then
        get stacked diagnostics v_sqlstate = returned_sqlstate;
        if v_sqlstate like '40%' or v_sqlstate like '53%' or v_sqlstate like '57%'
           or v_sqlstate like '08%' or v_sqlstate = '55P03' then
            raise;
        end if;
        v_rows := '[]'::jsonb;
        foreach v_flow_id in array v_flow_ids loop
            v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
                'version', (select c.active_version from public.flow_controls c where c.flow_id = v_flow_id),
                'outcome', 'motor_no_arranca', 'reason', 'error_interno:' || lower(v_sqlstate),
                'recovery_case_id', null));
        end loop;
    end;
    set constraints all deferred;

    insert into public.flow_engine_native_intent_admissions (
        submission_id, flow_id, version, ruta, outcome, reason, recovery_case_id)
    select v_submission_id, (row_value ->> 'flow_id')::uuid, (row_value ->> 'version')::integer,
           'nativo', row_value ->> 'outcome', row_value ->> 'reason',
           (row_value ->> 'recovery_case_id')::uuid
      from jsonb_array_elements(v_rows) as element(row_value);

    return query select v_admission_outcome, v_submission_id, v_intent_id, null::text, null::text,
        'nativo'::text, v_rows;
end;
$function$;

comment on function public.admit_and_route_registration_lead_precheckout_v1(
    text,text,integer,text,jsonb,jsonb,text,text) is
    'own-forms-v1 «Registro a un evento» (4.3, 20261127500000): admite el envio de un formulario propio de '
    'registro y lo prueba solo en los flujos nativos con dispara.registro igual a esa clave; nunca el legado. '
    'p_health_brake (meta_salud:<que>) frena antes de crear nada. Solo service_role.';

-- ===========================================================================
-- Bloque 6. Los permisos.
-- ===========================================================================

do $roles$
declare
    v_role text;
    c_function constant text :=
        'public.admit_and_route_registration_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,text)';
begin
    execute format('revoke all on function %s from public', c_function);
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function %s from %I', c_function, v_role);
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        execute format('grant execute on function %s to service_role', c_function);
    end if;
end;
$roles$;

commit;
