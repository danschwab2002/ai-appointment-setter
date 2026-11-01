-- flow-engine-native-intent-v1: la base del primer contacto nativo, el
-- disparador intencion (el formulario de GHL) del motor nativo (E-247, P20a de
-- M16; diseno: documentacion/2026-11-01-m16-diseno-primer-contacto-nativo.md).
--
-- Por que. El Automation Expert solo crea flujos nativos, y el nativo sabia la
-- compra, el carrito y el pago rechazado: el primer contacto tras el
-- formulario de Maribel (el scope maribel-primer-contacto y la politica
-- maribel-primer-contacto-formulario v2: gracia 1 h, 00:00-23:59 CDMX, vence a
-- 1 dia, un mensaje) no cabia. Contrato: docs/contracts/flow-engine-v2.md,
-- seccion 24 (2.12.0).
--
-- La pieza central: el nativo del formulario corta con el MISMO freno que el
-- legado, _portable_precheckout_stop_reason(intencion, contacto), en los tres
-- momentos del legado: al admitir (antes de crear nada), al reevaluar (por un
-- wrapper security definer) y al arrancar (con el lock de opt-out de las dos
-- formas del telefono). Asi la compra, el carrito, el pago rechazado, la baja
-- y la conversacion derivada cortan igual que hoy, sin salidas nuevas en el
-- esquema y sin le_gana_a en SQL.
--
-- Que cambia, por bloques:
--
-- 1. flow_engine_native_runs: trigger_event admite intencion, y la intencion
--    con consentimiento (audience_*) es obligatoria tambien con intencion.
-- 2. _flow_engine_native_trigger y _flow_engine_native_steps, reemplazados
--    enteros (son de una linea): intencion es un envio con nombre y producto.
--    _flow_engine_native_unsupported no cambia (pide formulario).
-- 3. _flow_engine_native_intent_audience (nueva): el permiso formulario de un
--    envio, el criterio de _portable_consented_intent_reason.
-- 4. admit_and_route_portable_lead_precheckout_v1 (nueva, la entrada): con la
--    misma firma y las mismas cinco columnas que
--    admit_and_plan_portable_lead_precheckout, mas ruta y flujos. Sin un flujo
--    nativo de intencion del producto, llama a esa funcion tal cual (o solo a
--    la admision, sin scope del legado): el camino de hoy, byte a byte. Con un
--    flujo, admite y prueba el nativo en una subtransaccion; si ningun flujo
--    lo toma (sin_reclamo), la deshace entera y llama al legado, que vuelve a
--    admitir y planifica como hoy.
-- 5. _flow_engine_admit_native_flow, con intencion: el permiso (bloque 3), el
--    doble caso con el legado (un caso landing precheckout_intent abierto, o un
--    toque precheckout_intent aceptado en 24 h: caso_del_legado_en_curso), la
--    repeticion de D21 (un caso abierto o un envio aceptado dentro de
--    otra_despues_de, de cualquier flujo de intencion), y el caso landing con
--    la intencion, el context extendido y el recovery_case_events.
-- 6. _flow_engine_authorize_native_tail, con intencion: los locks de opt-out
--    de las dos formas del telefono, y con ellos el freno y la audiencia.
-- 7. _flow_engine_native_intent_stop_reason (nueva, security definer, solo
--    service_role), y reevaluate_followup_action la llama solo para un caso con
--    context.flow_engine_trigger = intencion; los motivos cierran el caso.
-- 8. read_flow_engine_native_trigger_counts_v1 cuenta los formularios.
-- 9. read_automation_expert_native_history_v1: PRECHECKOUT_FORM_SUBMITTED.
-- 10. Los permisos.
-- 11. admit_portable_inbound_commercial_case_v1: un primer contacto nativo ya
--    condenado (carrito, pago, compra) no frena la adopcion de la respuesta,
--    como el del legado (20261009000200).
-- 12. flow_engine_native_intent_admissions (nueva): el resultado por envio y
--    por flujo, solo ids y codigos.
-- (El bloque 13 del diseno, la oferta del link, no va: el link del agente
-- elige la oferta por la intencion viva y el envio nativo no arma boton.)
--
-- Lo que no cambia. No se redefine ninguna funcion del primer contacto del
-- legado. Las ramas nuevas de las funciones compartidas preguntan por
-- intencion, que hoy no existe en ninguna base. El bridge todavia no llama a
-- la entrada nueva (P20b, detras de FLOW_ENGINE_NATIVE_INTENT_ENABLED).
--
-- Lo que no hace: no crea roles, no cambia el ACL de lo que ya existe y no
-- toca filas. Correrla dos veces aborta con 55000 sin cambiar nada. Los locks
-- (R9): la tabla del bloque 12 toma SHARE ROW EXCLUSIVE sobre
-- precheckout_submissions, flow_controls y recovery_cases por sus FK, y el
-- bloque 1, al final, ACCESS EXCLUSIVE sobre flow_engine_native_runs; todo con
-- lock_timeout de 5 s y fuera del horario de envios.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_engine_native_payment_failure_audience(uuid,uuid,text,text,text)') is null
       or to_regprocedure('public.admit_and_plan_portable_lead_precheckout(text,text,integer,text,jsonb,jsonb,text,integer)') is null
       or to_regprocedure('public._portable_precheckout_stop_reason(uuid,uuid)') is null
       or to_regprocedure('public._portable_checkout_link_purchase_exists(text,text,uuid,text,text)') is null
       or to_regprocedure('public._portable_consented_intent_reason(uuid,uuid,text)') is null
       or to_regprocedure('public.reject_flow_engine_native_mutation()') is null
       or to_regprocedure('public._flow_engine_duration_minutes(jsonb)') is null
       or to_regprocedure('public._whatsapp_phone_variants(text)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_intent_missing';
    end if;
    if to_regprocedure('public.admit_and_route_portable_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,integer)') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_native_intent_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla, con la misma verificacion para todos.
create function pg_temp.flow_engine_native_intent_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_intent_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_intent_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_intent_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_intent_definition_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

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
    -- flow_engine_native_intent: y desde 20261101000100 el formulario.
    select case
        when p_definition -> 'dispara' = '{"evento": "compra"}'::jsonb then 'compra'
        when p_definition -> 'dispara' = '{"evento": "carrito"}'::jsonb then 'carrito'
        when p_definition -> 'dispara' = '{"evento": "pago_fallido"}'::jsonb then 'pago_fallido'
        when p_definition -> 'dispara' = '{"evento": "intencion"}'::jsonb then 'intencion'
    end
$function$;

-- La forma de los pasos de un flujo nativo: sale del disparador. Los cuatro
-- son un solo envio, con las variables que el bridge sabe llenar (variables_de,
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
        -- flow_engine_native_intent: el primer contacto del formulario, de un toque (20261101000100).
        when 'nativo:intencion' then
            '[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "variables_de": ["nombre", "producto"], "coupon_variables": null}]'::jsonb
    end
$function$;

-- ===========================================================================
-- Bloque 3. El permiso del formulario.
-- ===========================================================================

-- El permiso formulario de un envio: la intencion del envio, de la instancia
-- y de la oferta, con el consentimiento de hoy para ese destino
-- (_portable_consented_intent_reason: viva, autorizada, el telefono del
-- contacto, sin baja previa y un envio 1.1.0 con los dos consentimientos y la
-- copia del binding). Devuelve el envio consentido mas nuevo de la intencion:
-- la admision exige que sea el que dispara (el legado:
-- precheckout_submission_not_consented); la reevaluacion y el arranque, no
-- (el legado tampoco: _lancemos_pilot_audience_intent). reason es null si
-- entra; si no, baja, ya_no_entra o no_entra:permiso.
create function public._flow_engine_native_intent_audience(
    p_submission_id uuid, p_purchase_intent_id uuid, p_contact_id uuid, p_offer text, p_destination text,
    p_tenant_ref text,
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
    reason := 'no_entra:permiso';
    select intent.* into v_intent
      from public.purchase_intents intent
      join public.purchase_intent_submissions link on link.purchase_intent_id = intent.id
     where intent.id = p_purchase_intent_id and link.submission_id = p_submission_id;
    if not found or p_offer is null or v_intent.tenant_ref is distinct from p_tenant_ref
       or v_intent.offer_ref is distinct from p_offer then
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
-- Bloque 12. El resultado nativo por envio (antes de la entrada, que lo usa).
-- ===========================================================================

-- Una fila por envio y por flujo de intencion que la entrada probo. ruta dice
-- quien se quedo con el envio: nativo (arranco o no arranca por un motivo del
-- nativo; el legado no planifica) o legado (sin_reclamo: lo planifico el
-- legado, que deja su renglon en portable_precheckout_first_contact_plans).
-- Solo ids y codigos. Un reenvio duplicate lee de aca. Solo suma.
create table public.flow_engine_native_intent_admissions (
    submission_id uuid not null references public.precheckout_submissions (id) on delete restrict,
    flow_id uuid not null references public.flow_controls (flow_id) on delete restrict,
    version integer check (version > 0),
    ruta text not null check (ruta in ('nativo', 'legado')),
    outcome text not null check (outcome in ('motor_arranco', 'motor_no_arranca')),
    reason text check (reason ~ '^[a-z0-9_:]{1,80}$'),
    recovery_case_id uuid references public.recovery_cases (id) on delete restrict,
    created_at timestamptz not null default clock_timestamp(),
    primary key (submission_id, flow_id),
    constraint flow_engine_native_intent_admissions_reason_by_outcome
        check ((outcome = 'motor_arranco') = (reason is null)),
    constraint flow_engine_native_intent_admissions_case_by_outcome
        check (recovery_case_id is null or outcome = 'motor_arranco')
);

alter table public.flow_engine_native_intent_admissions enable row level security;

create trigger flow_engine_native_intent_admissions_append_only
before update or delete on public.flow_engine_native_intent_admissions
for each row execute function public.reject_flow_engine_native_mutation();
create trigger flow_engine_native_intent_admissions_no_truncate
before truncate on public.flow_engine_native_intent_admissions
for each statement execute function public.reject_flow_engine_native_mutation();

-- Los motivos con los que un flujo de intencion no se queda con el envio: el
-- legado decide (el criterio de NATIVE_CART_UNCLAIMED_REASONS del bridge, mas,
-- mientras dure el corte, las fallas del motor).
create function public._flow_engine_native_intent_unclaimed(p_reason text)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(p_reason in ('sin_flujo', 'fuera_de_cohorte', 'flujo_pausado', 'flujo_apagado',
                                 'caso_del_legado_en_curso', 'flow_engine_pool_unknown')
                    or p_reason like 'motor\_nativo:%'
                    or p_reason like 'politica\_compilada:%'
                    or p_reason like 'error\_interno:%', false)
$function$;

-- ===========================================================================
-- Bloque 4. La entrada del formulario.
-- ===========================================================================

create function public.admit_and_route_portable_lead_precheckout_v1(
    p_tenant_ref text, p_funnel_ref text, p_binding_version integer, p_external_submission_id text,
    p_raw_payload jsonb, p_canonical_payload jsonb, p_legacy_scope_key text, p_legacy_scope_version integer)
returns table(outcome text, submission_id uuid, purchase_intent_id uuid, plan_outcome text, plan_reason text,
              ruta text, flujos jsonb)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_flow_ids uuid[];
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
    v_row jsonb;
    v_touch_flow uuid;
    v_touch_version integer;
    v_native boolean := false;
    v_to_legacy boolean := false;
    v_duplicate boolean := false;
    v_legacy record;
    v_sqlstate text;
    v_message text;
    v_detail text;
begin
    if (p_legacy_scope_key is null) <> (p_legacy_scope_version is null) then
        raise exception using errcode = '22023', message = 'invalid_pilot_plan_parameters';
    end if;

    -- Los flujos nativos de intencion del producto (el funnel del binding),
    -- como admit_flow_engine_event_v1: ruta nativa, version activa que dispara
    -- intencion, no apagados.
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
           and d.definition #>> '{dispara,evento}' = 'intencion';
    end if;
    -- Sin flujos de intencion del producto y sin ningun recorrido nativo de
    -- intencion en la base (ATT1 hoy), el legado tal cual. Si hay recorridos
    -- (otro producto, o el flujo ya apagado), igual se mira el toque nativo de
    -- 24 h antes de dejarle el envio al legado (D21: el legado no lo ve).
    v_to_legacy := coalesce(cardinality(v_flow_ids), 0) = 0
        and not exists (select 1 from public.flow_engine_native_runs native_any
                         where native_any.trigger_event = 'intencion');

    if not v_to_legacy then
        -- (a) El orden de hoy: el control del scope del legado, despues los
        -- flujos (FOR SHARE, en orden de flow_id), despues la intencion y el
        -- contacto (adentro de la admision y del plan).
        if p_legacy_scope_key is not null then
            perform 1 from public.pilot_runtime_controls control
             where control.scope_key = p_legacy_scope_key
               for update;
        end if;
        v_flow_ids := coalesce(v_flow_ids, array[]::uuid[]);
        perform 1 from public.flow_engine_routes r where r.flow_id = any(v_flow_ids) order by r.flow_id for share;
        perform 1 from public.flow_controls c where c.flow_id = any(v_flow_ids) order by c.flow_id for share;

        -- (b) a (g): la admision y el nativo en una subtransaccion. Si ningun
        -- flujo se queda con el envio, se deshace entera y decide el legado.
        begin
            select admission.outcome, admission.submission_id, admission.purchase_intent_id
              into strict v_admission_outcome, v_submission_id, v_intent_id
              from public.admit_portable_observed_lead_precheckout(
                  p_tenant_ref, p_funnel_ref, p_binding_version, p_external_submission_id,
                  p_raw_payload, p_canonical_payload) admission;

            if v_admission_outcome <> 'inserted' then
                -- Un reenvio: lo que se decidio para ese envio. Si lo tomo el
                -- nativo, de aca; si no, el legado devuelve su renglon.
                v_duplicate := true;
                select coalesce(jsonb_agg(jsonb_build_object(
                           'flow_id', x.flow_id, 'version', x.version, 'outcome', x.outcome,
                           'reason', x.reason, 'recovery_case_id', x.recovery_case_id) order by x.flow_id),
                       '[]'::jsonb)
                  into v_rows
                  from public.flow_engine_native_intent_admissions x
                 where x.submission_id = v_submission_id and x.ruta = 'nativo';
                if jsonb_array_length(v_rows) = 0 then
                    raise exception using errcode = 'P2047', message = 'flow_engine_native_intent_to_legacy';
                end if;
                v_native := true;
            else
                select intent.* into strict v_intent from public.purchase_intents intent where intent.id = v_intent_id;
                select submission.* into strict v_submission
                  from public.precheckout_submissions submission where submission.id = v_submission_id;
                v_submitted_at := (v_submission.canonical_payload #>> '{submitted_at}')::timestamptz;

                -- El contacto, sin crearlo: decide la cohorte.
                select found.contact_id, found.reason_code into v_found_contact, v_find_reason
                  from public._find_portable_precheckout_contact(v_intent_id) found;

                -- El toque nativo de las ultimas 24 h (D21 del legado, que solo ve
                -- el ancla precheckout_intent): un envio aceptado o de resultado
                -- desconocido de un recorrido nativo de intencion de la persona,
                -- de cualquier flujo y producto. Si el envio iba a quedar para el
                -- legado, es del nativo: ya_disparado, sin plan del legado.
                if v_found_contact is not null then
                    select run.flow_id, run.version into v_touch_flow, v_touch_version
                      from public.flow_engine_native_runs native
                      join public.recovery_cases rc on rc.id = native.recovery_case_id
                      join public.flow_engine_runs run on run.recovery_case_id = native.recovery_case_id
                      join public.scheduled_actions touched on touched.recovery_case_id = native.recovery_case_id
                     where native.trigger_event = 'intencion' and rc.contact_id = v_found_contact
                       and touched.anchor_type = 'flow_engine_event'
                       and touched.status in ('accepted_by_chatwoot', 'delivery_unknown')
                       and coalesce(touched.executed_at, touched.updated_at) > clock_timestamp() - interval '24 hours'
                     order by coalesce(touched.executed_at, touched.updated_at) desc
                     limit 1;
                end if;

                -- Quien reclama: prendido siempre; cohorte solo con el contacto
                -- adentro. Pausado y fuera de la cohorte son del legado.
                foreach v_flow_id in array v_flow_ids loop
                    select c.* into strict v_control from public.flow_controls c where c.flow_id = v_flow_id;
                    if v_control.state = 'pausado' then
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
                if cardinality(v_claimers) = 0 and v_touch_flow is null then
                    raise exception using errcode = 'P2047', message = 'flow_engine_native_intent_to_legacy';
                end if;

                if cardinality(v_claimers) = 0 then
                    -- Solo el toque nativo de 24 h: lo registra el bloque de abajo.
                    v_reason := null;
                    v_native := false;
                else
                -- (c) Lo que se decide sin contacto, antes de crear nada: la
                -- intencion viva y autorizada, el consentimiento del envio y los
                -- frenos del legado sin contacto (el orden de
                -- admit_and_plan_portable_lead_precheckout, pasos 1 y 4).
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
                if v_reason is null then
                    v_reason := public._portable_precheckout_stop_reason(v_intent_id, null);
                end if;

                -- (d) El contacto: creado solo si algun flujo esta prendido (en
                -- cohorte no se crean contactos, como el legado). Con el
                -- contacto, los frenos otra vez.
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
                    -- Un motivo del nativo: el legado tampoco planificaria.
                    foreach v_flow_id in array v_claimers loop
                        select c.* into strict v_control from public.flow_controls c where c.flow_id = v_flow_id;
                        perform public._flow_engine_record_start_rejection(
                            v_flow_id, v_control.active_version, v_submission_id, 'admision', v_reason);
                        v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
                            'version', v_control.active_version, 'outcome', 'motor_no_arranca',
                            'reason', public._flow_engine_reason(v_reason, 'motivo_invalido'),
                            'recovery_case_id', null));
                    end loop;
                    v_native := true;
                else
                    -- (e) El destino en la forma de la identidad de WhatsApp que ya
                    -- exista (la del contacto, primero la activa y la textual).
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

                    -- (f) El ancla: un webhook_events de fuente system por envio,
                    -- processed y con payload de ids y creation_date (lo ordena el
                    -- trigger de compra conocida). Distinta de la del legado.
                    insert into public.webhook_events (
                        source, external_event_id, event_type, payload, processing_status, processed_at)
                    values (
                        'system', 'flow-engine-intent:' || v_submission_id::text, 'PRECHECKOUT_FORM_SUBMITTED',
                        jsonb_build_object('purchase_intent_id', v_intent_id,
                                           'precheckout_submission_id', v_submission_id,
                                           'creation_date', floor(extract(epoch from v_submitted_at) * 1000)::bigint),
                        'processed', clock_timestamp())
                    returning * into v_event;

                    -- (g) Cada flujo en su subtransaccion. Un error transitorio se
                    -- relanza: deshace todo y GHL reintenta (el bucle de
                    -- admit_flow_engine_event_v1 los atrapa todos, y aca eso
                    -- consumiria el formulario sin caso).
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
                                -- (i) Los triggers diferidos del caso y la accion corren
                                -- aca: si uno falla se deshace el flujo, no la admision.
                                set constraints all immediate;
                            exception when others then
                                get stacked diagnostics v_sqlstate = returned_sqlstate;
                                if v_sqlstate like '40%' or v_sqlstate like '53%' or v_sqlstate like '57%'
                                   or v_sqlstate like '08%' or v_sqlstate = '55P03' then
                                    raise;
                                end if;
                                perform public._flow_engine_record_start_rejection(
                                    v_flow_id, v_control.active_version, v_event.id, 'admision',
                                    'error_interno:' || lower(v_sqlstate));
                                select 'motor_no_arranca'::text as outcome,
                                       'error_interno:' || lower(v_sqlstate) as reason,
                                       null::uuid as recovery_case_id
                                  into v_one;
                            end;
                            set constraints all deferred;
                        end if;
                        v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
                            'version', v_control.active_version, 'outcome', v_one.outcome, 'reason', v_one.reason,
                            'recovery_case_id', v_one.recovery_case_id));
                        if v_one.outcome = 'motor_arranco'
                           or not public._flow_engine_native_intent_unclaimed(v_one.reason) then
                            v_native := true;
                        end if;
                    end loop;
                end if;

                end if;

                if not v_native and v_touch_flow is not null then
                    -- Nadie se quedo con el envio y el legado no ve el toque nativo:
                    -- ya_disparado, del flujo que mando (su fila reemplaza la de ese flujo).
                    select coalesce(jsonb_agg(element.row_value), '[]'::jsonb) into v_rows
                      from jsonb_array_elements(v_rows) as element(row_value)
                     where element.row_value ->> 'flow_id' is distinct from v_touch_flow::text;
                    v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_touch_flow,
                        'version', v_touch_version, 'outcome', 'motor_no_arranca', 'reason', 'ya_disparado',
                        'recovery_case_id', null));
                    perform public._flow_engine_record_start_rejection(
                        v_touch_flow, v_touch_version, v_submission_id, 'admision', 'ya_disparado');
                    v_native := true;
                end if;
                if not v_native then
                    raise exception using errcode = 'P2047', message = 'flow_engine_native_intent_to_legacy';
                end if;
                insert into public.flow_engine_native_intent_admissions (
                    submission_id, flow_id, version, ruta, outcome, reason, recovery_case_id)
                select v_submission_id, (row_value ->> 'flow_id')::uuid, (row_value ->> 'version')::integer,
                       'nativo', row_value ->> 'outcome', row_value ->> 'reason',
                       (row_value ->> 'recovery_case_id')::uuid
                  from jsonb_array_elements(v_rows) as element(row_value);
            end if;
        exception when others then
            get stacked diagnostics v_sqlstate = returned_sqlstate, v_message = message_text;
            if v_sqlstate like '40%' or v_sqlstate like '53%' or v_sqlstate like '57%'
               or v_sqlstate like '08%' or v_sqlstate = '55P03' then
                raise;
            end if;
            if not (v_sqlstate = 'P2047' and v_message = 'flow_engine_native_intent_to_legacy') then
                -- Una falla del nativo fuera de un flujo: mientras dure el
                -- corte, el envio es del legado (la admision la rehace el).
                v_rows := '[]'::jsonb;
                foreach v_flow_id in array v_flow_ids loop
                    v_rows := v_rows || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
                        'version', (select c.active_version from public.flow_controls c where c.flow_id = v_flow_id),
                        'outcome', 'motor_no_arranca', 'reason', 'error_interno:' || lower(v_sqlstate),
                        'recovery_case_id', null));
                end loop;
            end if;
            v_native := false;
            v_to_legacy := true;
        end;
        set constraints all deferred;

        if v_native then
            return query select v_admission_outcome, v_submission_id, v_intent_id, null::text, null::text,
                'nativo'::text, v_rows;
            return;
        end if;
    end if;

    -- (h) El legado, como hoy: con su scope, la admision y el plan
    -- (admit_and_plan_portable_lead_precheckout, sin redefinir); sin scope,
    -- solo la admision.
    if p_legacy_scope_key is not null then
        select legacy.outcome, legacy.submission_id, legacy.purchase_intent_id,
               legacy.plan_outcome, legacy.plan_reason
          into v_legacy
          from public.admit_and_plan_portable_lead_precheckout(
              p_tenant_ref, p_funnel_ref, p_binding_version, p_external_submission_id,
              p_raw_payload, p_canonical_payload, p_legacy_scope_key, p_legacy_scope_version) legacy;
    else
        select admission.outcome, admission.submission_id, admission.purchase_intent_id,
               null::text as plan_outcome, null::text as plan_reason
          into v_legacy
          from public.admit_portable_observed_lead_precheckout(
              p_tenant_ref, p_funnel_ref, p_binding_version, p_external_submission_id,
              p_raw_payload, p_canonical_payload) admission;
    end if;

    -- Lo que el nativo dijo de un envio nuevo que dejo al legado: su fila, y el
    -- rechazo de la admision salvo fuera_de_cohorte (contrato 4.3).
    if v_legacy.outcome = 'inserted' and not v_duplicate and jsonb_array_length(v_rows) > 0 then
        for v_row in select element.row_value from jsonb_array_elements(v_rows) as element(row_value) loop
            insert into public.flow_engine_native_intent_admissions (
                submission_id, flow_id, version, ruta, outcome, reason, recovery_case_id)
            values (v_legacy.submission_id, (v_row ->> 'flow_id')::uuid, (v_row ->> 'version')::integer,
                    'legado', 'motor_no_arranca', v_row ->> 'reason', null);
            if v_row ->> 'reason' is distinct from 'fuera_de_cohorte' and v_row ->> 'version' is not null then
                perform public._flow_engine_record_start_rejection(
                    (v_row ->> 'flow_id')::uuid, (v_row ->> 'version')::integer, v_legacy.submission_id,
                    'admision', v_row ->> 'reason');
            end if;
        end loop;
    end if;

    return query select v_legacy.outcome, v_legacy.submission_id, v_legacy.purchase_intent_id,
        v_legacy.plan_outcome, v_legacy.plan_reason, 'legado'::text,
        case when v_duplicate then '[]'::jsonb else v_rows end;
end;
$function$;

-- ===========================================================================
-- Bloque 7. El freno en la reevaluacion (antes de los parches, que la llaman).
-- ===========================================================================

-- El freno del legado para un caso nativo de intencion, o la perdida de la
-- audiencia (precheckout_authorization_lost, como
-- reevaluate_portable_precheckout_action). Security definer porque
-- reevaluate_followup_action es security invoker y corre como service_role,
-- al que se le revoco _portable_precheckout_stop_reason. Null para todo caso
-- que no sea un recorrido nativo de intencion.
create function public._flow_engine_native_intent_stop_reason(p_case_id uuid)
returns text
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.recovery_cases%rowtype;
    v_native public.flow_engine_native_runs%rowtype;
    v_destination text;
    v_tenant text;
    v_reason text;
begin
    select rc.* into v_case from public.recovery_cases rc where rc.id = p_case_id;
    select native.* into v_native from public.flow_engine_native_runs native where native.recovery_case_id = p_case_id;
    if v_case.id is null or v_native.recovery_case_id is null or v_native.trigger_event <> 'intencion' then
        return null;
    end if;
    v_reason := public._portable_precheckout_stop_reason(v_native.audience_purchase_intent_id, v_case.contact_id);
    if v_reason is not null then
        return v_reason;
    end if;
    select identity_row.external_user_id into v_destination
      from public.channel_identities identity_row
     where identity_row.id = v_case.selected_channel_identity_id and identity_row.contact_id = v_case.contact_id;
    select c.tenant_ref into v_tenant
      from public.flow_engine_runs run join public.flow_controls c on c.flow_id = run.flow_id
     where run.recovery_case_id = p_case_id;
    if v_destination is null or (
        select audience.reason
          from public._flow_engine_native_intent_audience(
              v_native.audience_precheckout_submission_id, v_native.audience_purchase_intent_id,
              v_case.contact_id, v_native.offer_code, v_destination, v_tenant) audience) is not null then
        return 'precheckout_authorization_lost';
    end if;
    return null;
end;
$function$;

-- ===========================================================================
-- Bloques 5, 6, 7, 8, 9 y 11. Los reemplazos sobre la definicion vigente.
-- ===========================================================================

do $patch$
declare
    c_admit constant text :=
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)';
    c_tail constant text :=
        'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)';
    c_reevaluate constant text :=
        'public.reevaluate_followup_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)';
    c_counts constant text := 'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)';
    c_adoption constant text := 'public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)';
begin
    -- 5b. El doble caso con el primer contacto del legado, despues del del pago.
    perform pg_temp.flow_engine_native_intent_patch(c_admit,
        E'    -- flow_engine_native_payment_failure_double_case: end\n',
        E'    -- flow_engine_native_payment_failure_double_case: end\n'
        || E'\n'
        || E'    -- flow_engine_native_intent_double_case: begin\n'
        || E'    -- El doble caso con el primer contacto del legado (20261101000100, P20 de\n'
        || E'    -- M16): un caso landing del formulario abierto que no es nativo, o un toque\n'
        || E'    -- del legado aceptado (o de resultado desconocido) en las ultimas 24 h (D21\n'
        || E'    -- de _plan_portable_precheckout_first_contact). El envio es del legado, que\n'
        || E'    -- dice precheckout_contact_already_planned (el bridge: sin_reclamo).\n'
        || E'    if public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
        || E'       and (exists (\n'
        || E'               select 1\n'
        || E'                 from public.recovery_cases legacy_case\n'
        || E'                where legacy_case.contact_id = p_contact_id\n'
        || E'                  and legacy_case.source = ''landing''\n'
        || E'                  and legacy_case.context ->> ''trigger_kind'' = ''precheckout_intent''\n'
        || E'                  and legacy_case.status in (''grace_period'', ''active'', ''paused'')\n'
        || E'                  and not exists (\n'
        || E'                      select 1 from public.flow_engine_native_runs native_case\n'
        || E'                       where native_case.recovery_case_id = legacy_case.id))\n'
        || E'            or exists (\n'
        || E'               select 1\n'
        || E'                 from public.scheduled_actions touched\n'
        || E'                 join public.recovery_cases touched_case on touched_case.id = touched.recovery_case_id\n'
        || E'                where touched_case.contact_id = p_contact_id\n'
        || E'                  and touched.anchor_type = ''precheckout_intent''\n'
        || E'                  and touched.status in (''accepted_by_chatwoot'', ''delivery_unknown'')\n'
        || E'                  and coalesce(touched.executed_at, touched.updated_at) > v_now - interval ''24 hours'')) then\n'
        || E'        perform public._flow_engine_record_start_rejection(\n'
        || E'            p_control.flow_id, v_definition.version, p_event.id, ''admision'', ''caso_del_legado_en_curso'');\n'
        || E'        return query select ''motor_no_arranca''::text, ''caso_del_legado_en_curso''::text,\n'
        || E'            null::uuid, null::uuid, null::uuid, null::boolean;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_intent_double_case: end\n',
        'flow_engine_native_intent_double_case: begin');

    -- 5a. El permiso del formulario, antes del de la compra.
    perform pg_temp.flow_engine_native_intent_patch(c_admit,
        E'    elsif public._flow_engine_native_trigger(v_definition.definition) is distinct from ''compra''\n'
        || E'       or p_event.event_type is distinct from ''PURCHASE_APPROVED'' then\n',
        E'    -- flow_engine_native_intent_permission: el formulario (20261101000100), con\n'
        || E'    -- la intencion del envio del ancla, que tiene que ser el envio consentido.\n'
        || E'    elsif public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
        || E'       and p_event.source = ''system'' and p_event.event_type = ''PRECHECKOUT_FORM_SUBMITTED'' then\n'
        || E'        select audience.purchase_intent_id, audience.precheckout_submission_id, audience.reason\n'
        || E'          into v_audience_intent, v_audience_submission, v_reason\n'
        || E'          from public._flow_engine_native_intent_audience(\n'
        || E'              (p_event.payload ->> ''precheckout_submission_id'')::uuid,\n'
        || E'              (p_event.payload ->> ''purchase_intent_id'')::uuid,\n'
        || E'              p_contact_id, p_offer, p_destination, p_control.tenant_ref) audience;\n'
        || E'        if v_reason is null\n'
        || E'           and v_audience_submission is distinct from (p_event.payload ->> ''precheckout_submission_id'')::uuid then\n'
        || E'            v_reason := ''no_entra:permiso'';\n'
        || E'        end if;\n'
        || E'    elsif public._flow_engine_native_trigger(v_definition.definition) is distinct from ''compra''\n'
        || E'       or p_event.event_type is distinct from ''PURCHASE_APPROVED'' then\n',
        'flow_engine_native_intent_permission');

    -- 5c. La repeticion de D21 con el formulario; la generica queda para los demas.
    perform pg_temp.flow_engine_native_intent_patch(c_admit,
        E'    if v_reason is null then\n'
        || E'        v_after := case when v_definition.definition #> ''{topes,repeticion,otra_despues_de}'' = ''"nunca"''::jsonb\n',
        E'    -- flow_engine_native_intent_repeat: begin\n'
        || E'    -- El formulario (20261101000100, D2 del diseno): la regla de D21 del legado.\n'
        || E'    -- Frena un recorrido nativo de intencion de la persona (de cualquier flujo y\n'
        || E'    -- producto) con el caso abierto (ya_en_curso), o con un envio aceptado o de\n'
        || E'    -- resultado desconocido dentro de otra_despues_de, contado desde el envio y\n'
        || E'    -- no desde el arranque (ya_disparado). Un recorrido que el carrito corto sin\n'
        || E'    -- mandar nada no frena.\n'
        || E'    if v_reason is null\n'
        || E'       and public._flow_engine_native_trigger(v_definition.definition) = ''intencion'' then\n'
        || E'        v_after := case when v_definition.definition #> ''{topes,repeticion,otra_despues_de}'' = ''"nunca"''::jsonb\n'
        || E'                        then null\n'
        || E'                        else public._flow_engine_duration_minutes(\n'
        || E'                            v_definition.definition #> ''{topes,repeticion,otra_despues_de}'') end;\n'
        || E'        select case when rc.status in (''grace_period'', ''active'', ''paused'') then ''ya_en_curso''\n'
        || E'                    else ''ya_disparado'' end\n'
        || E'          into v_reason\n'
        || E'          from public.flow_engine_native_runs native\n'
        || E'          join public.recovery_cases rc on rc.id = native.recovery_case_id\n'
        || E'         where native.trigger_event = ''intencion'' and rc.contact_id = p_contact_id\n'
        || E'           and (v_definition.definition #>> ''{topes,repeticion,una_por}'' = ''persona''\n'
        || E'                or native.offer_code is not distinct from p_offer)\n'
        || E'           and (rc.status in (''grace_period'', ''active'', ''paused'')\n'
        || E'                or exists (\n'
        || E'                    select 1 from public.scheduled_actions touched\n'
        || E'                     where touched.recovery_case_id = rc.id\n'
        || E'                       and touched.anchor_type = ''flow_engine_event''\n'
        || E'                       and touched.status in (''accepted_by_chatwoot'', ''delivery_unknown'')\n'
        || E'                       and (v_after is null\n'
        || E'                            or coalesce(touched.executed_at, touched.updated_at)\n'
        || E'                               > v_now - v_after * interval ''1 minute'')))\n'
        || E'         order by (rc.status in (''grace_period'', ''active'', ''paused'')) desc\n'
        || E'         limit 1;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_intent_repeat: end\n'
        || E'    if v_reason is null\n'
        || E'       and public._flow_engine_native_trigger(v_definition.definition) is distinct from ''intencion'' then\n'
        || E'        v_after := case when v_definition.definition #> ''{topes,repeticion,otra_despues_de}'' = ''"nunca"''::jsonb\n',
        'flow_engine_native_intent_repeat: begin');

    -- 5d. El caso: landing, con la intencion y el context del legado.
    perform pg_temp.flow_engine_native_intent_patch(c_admit,
        E'        status, lead_stage, grace_expires_at, policy_key, policy_version, context)\n',
        E'        status, lead_stage, grace_expires_at, policy_key, policy_version, context,\n'
        || E'        -- flow_engine_native_intent_case: la intencion del formulario (20261101000100).\n'
        || E'        hotmart_purchase_intent_id)\n',
        'flow_engine_native_intent_case');
    perform pg_temp.flow_engine_native_intent_patch(c_admit,
        E'        p_contact_id, p_event.id, p_event.source, v_binding.hotmart_product_id::text, v_binding.product_name, null,\n',
        E'        p_contact_id, p_event.id,\n'
        || E'        -- flow_engine_native_intent_source: el formulario es landing (la check de\n'
        || E'        -- recovery_cases.source no admite system).\n'
        || E'        case when public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
        || E'             then ''landing'' else p_event.source end,\n'
        || E'        v_binding.hotmart_product_id::text, v_binding.product_name, null,\n',
        'flow_engine_native_intent_source');
    perform pg_temp.flow_engine_native_intent_patch(c_admit,
        E'                           ''flow_engine_offer'', p_offer))\n'
        || E'    returning id into v_case_id;\n',
        E'                           ''flow_engine_offer'', p_offer)\n'
        || E'        -- flow_engine_native_intent_context: con el formulario, el context del\n'
        || E'        -- legado (el un vivo por persona del legado lo ve: trigger_kind), sin pisar\n'
        || E'        -- flow_engine_offer.\n'
        || E'        || case when public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
        || E'                then jsonb_build_object(''trigger_kind'', ''precheckout_intent'',\n'
        || E'                                        ''purchase_intent_id'', v_audience_intent,\n'
        || E'                                        ''precheckout_submission_id'', v_audience_submission,\n'
        || E'                                        ''precheckout_submitted_at'', p_moment)\n'
        || E'                else ''{}''::jsonb end,\n'
        || E'        case when public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
        || E'             then v_audience_intent end)\n'
        || E'    returning id into v_case_id;\n'
        || E'    if public._flow_engine_native_trigger(v_definition.definition) = ''intencion'' then\n'
        || E'        insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)\n'
        || E'        values (v_case_id, p_event.id, ''precheckout_intent'', p_moment);\n'
        || E'    end if;\n',
        'flow_engine_native_intent_context');

    -- 6. El arranque: los locks de opt-out y, con ellos, el freno y la audiencia.
    perform pg_temp.flow_engine_native_intent_patch(c_tail,
        E'    v_id uuid;\nbegin\n',
        E'    v_id uuid;\n'
        || E'    -- flow_engine_native_intent_tail_vars (20261101000100)\n'
        || E'    v_intent_variant text;\n'
        || E'    v_intent_stop text;\n'
        || E'begin\n',
        'flow_engine_native_intent_tail_vars');
    perform pg_temp.flow_engine_native_intent_patch(c_tail,
        E'    -- flow_engine_native_cart_recheck: end\n',
        E'    -- flow_engine_native_cart_recheck: end\n'
        || E'    -- flow_engine_native_intent_recheck: begin\n'
        || E'    -- El formulario (20261101000100): el lock de opt-out de las dos formas del\n'
        || E'    -- telefono, en orden, como mark_portable_precheckout_request_started; con\n'
        || E'    -- el, el freno del legado y la audiencia. El rechazo no inserta la\n'
        || E'    -- autorizacion: no consume cupo.\n'
        || E'    if v_native.trigger_event = ''intencion'' then\n'
        || E'        for v_intent_variant in\n'
        || E'            select variant.phone\n'
        || E'              from unnest(public._whatsapp_phone_variants(v_identity.external_user_id)) as variant(phone)\n'
        || E'             order by variant.phone\n'
        || E'        loop\n'
        || E'            perform pg_advisory_xact_lock(hashtextextended(\n'
        || E'                concat_ws('':'', ''chatwoot-opt-out-user'', v_native.chatwoot_account_id, v_intent_variant), 0));\n'
        || E'        end loop;\n'
        || E'        perform 1 from public.purchase_intents intent\n'
        || E'         where intent.id = v_native.audience_purchase_intent_id\n'
        || E'         for share;\n'
        || E'        v_intent_stop := public._portable_precheckout_stop_reason(\n'
        || E'            v_native.audience_purchase_intent_id, p_case.contact_id);\n'
        || E'        if v_intent_stop is null and (\n'
        || E'            select audience.reason\n'
        || E'              from public._flow_engine_native_intent_audience(\n'
        || E'                  v_native.audience_precheckout_submission_id, v_native.audience_purchase_intent_id,\n'
        || E'                  p_case.contact_id, v_native.offer_code, v_identity.external_user_id,\n'
        || E'                  p_control.tenant_ref) audience) is not null then\n'
        || E'            v_intent_stop := ''precheckout_authorization_lost'';\n'
        || E'        end if;\n'
        || E'        if v_intent_stop is not null then\n'
        || E'            return query select ''rechazado''::text, v_intent_stop, null::uuid, p_control.generation, false;\n'
        || E'            return;\n'
        || E'        end if;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_intent_recheck: end\n',
        'flow_engine_native_intent_recheck: begin');

    -- 7. La reevaluacion: el freno del legado, solo para un caso de intencion.
    perform pg_temp.flow_engine_native_intent_patch(c_reevaluate,
        E'    v_replay_data jsonb;\n',
        E'    v_replay_data jsonb;\n'
        || E'    -- flow_engine_native_intent_reevaluation_var (20261101000100)\n'
        || E'    v_native_intent_stop text;\n',
        'flow_engine_native_intent_reevaluation_var');
    perform pg_temp.flow_engine_native_intent_patch(c_reevaluate,
        E'    if v_contact.contact_permission in (''opted_out'', ''blocked'', ''restricted'')\n',
        E'    -- flow_engine_native_intent_reevaluation_once: el freno del formulario, una\n'
        || E'    -- sola vez y solo para un caso de intencion (20261101000100).\n'
        || E'    if v_case.context ->> ''flow_engine_trigger'' = ''intencion'' then\n'
        || E'        v_native_intent_stop := public._flow_engine_native_intent_stop_reason(v_case.id);\n'
        || E'    end if;\n'
        || E'    if v_contact.contact_permission in (''opted_out'', ''blocked'', ''restricted'')\n',
        'flow_engine_native_intent_reevaluation_once');
    perform pg_temp.flow_engine_native_intent_patch(c_reevaluate,
        E'        v_reason := public._portable_case_purchase_stop_reason(v_case.id);\n',
        E'        v_reason := public._portable_case_purchase_stop_reason(v_case.id);\n'
        || E'    -- flow_engine_native_intent_reevaluation: begin\n'
        || E'    -- El primer contacto nativo del formulario (20261101000100): los frenos del\n'
        || E'    -- legado (_portable_precheckout_stop_reason por el wrapper), calculado\n'
        || E'    -- arriba solo para un caso de intencion.\n'
        || E'    elsif v_case.status in (''grace_period'', ''active'')\n'
        || E'       and v_native_intent_stop is not null then\n'
        || E'        v_decision := ''cancel'';\n'
        || E'        v_reason := v_native_intent_stop;\n'
        || E'    -- flow_engine_native_intent_reevaluation: end\n',
        'flow_engine_native_intent_reevaluation: begin');
    perform pg_temp.flow_engine_native_intent_patch(c_reevaluate,
        E'            -- flow_engine_turned_off_close: end\n'
        || E'        ) then\n',
        E'            -- flow_engine_turned_off_close: end\n'
        || E'        )\n'
        || E'        -- flow_engine_native_intent_close: los frenos del formulario cierran el\n'
        || E'        -- caso nativo (cancelled, nunca won), como el legado (20261101000100).\n'
        || E'        or (v_case.context ->> ''flow_engine_trigger'' = ''intencion''\n'
        || E'            and v_reason in (''intent_purchased'', ''intent_purchase_ambiguous'', ''purchase_by_identity'',\n'
        || E'                             ''superseded_by_provider_event'', ''precheckout_prior_opt_out'',\n'
        || E'                             ''precheckout_conversation_handoff'', ''precheckout_intent_not_live'',\n'
        || E'                             ''precheckout_binding_unavailable'', ''precheckout_authorization_lost'')) then\n',
        'flow_engine_native_intent_close');

    -- 8. La simulacion de un item nuevo cuenta los formularios.
    perform pg_temp.flow_engine_native_intent_patch(c_counts,
        E'    if p_trigger is null or p_trigger not in (''compra'', ''carrito'', ''pago_fallido'') then\n',
        E'    -- flow_engine_native_intent_trigger: y el formulario (20261101000100).\n'
        || E'    if p_trigger is null or p_trigger not in (''compra'', ''carrito'', ''pago_fallido'', ''intencion'') then\n',
        'flow_engine_native_intent_trigger');
    perform pg_temp.flow_engine_native_intent_patch(c_counts,
        E'    select coalesce(array_agg(distinct b.hotmart_product_id::text), array[]::text[]) into v_product_ids\n',
        E'    -- flow_engine_native_intent_counts: begin\n'
        || E'    -- El formulario: los envios con los dos consentimientos del producto (el\n'
        || E'    -- funnel) en la ventana; las personas por email o por telefono.\n'
        || E'    if p_trigger = ''intencion'' then\n'
        || E'        select count(*)::integer,\n'
        || E'               count(distinct coalesce(nullif(lower(btrim(s.canonical_payload #>> ''{identity,email}'')), ''''),\n'
        || E'                                       public._whatsapp_phone_canonical(s.canonical_payload #>> ''{identity,phone}''),\n'
        || E'                                       s.id::text))::integer\n'
        || E'          into v_events, v_people\n'
        || E'          from public.precheckout_submissions s\n'
        || E'         where s.canonical_payload #>> ''{source,tenant_ref}'' = v_instance.o_tenant_ref\n'
        || E'           and s.canonical_payload #>> ''{source,funnel_ref}'' = p_product_ref\n'
        || E'           and s.received_at >= p_since\n'
        || E'           and s.contract_version = ''1.1.0'' and not s.provisional and s.provider_observed\n'
        || E'           and s.activation_authorized\n'
        || E'           and s.canonical_payload #>> ''{consent,whatsapp_contact}'' = ''true''\n'
        || E'           and s.canonical_payload #>> ''{consent,marketing_optin}'' = ''true'';\n'
        || E'        return jsonb_build_object(''contract'', ''flow-engine-v2'', ''trigger'', p_trigger,\n'
        || E'            ''product_ref'', p_product_ref, ''since'', p_since, ''until'', v_now,\n'
        || E'            ''events'', v_events, ''people'', v_people);\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_intent_counts: end\n'
        || E'    select coalesce(array_agg(distinct b.hotmart_product_id::text), array[]::text[]) into v_product_ids\n',
        'flow_engine_native_intent_counts: begin');

    -- 9. La historia del AE: el evento que arranco un recorrido del formulario.
    perform pg_temp.flow_engine_native_intent_patch(
        'public.read_automation_expert_native_history_v1(jsonb,uuid)',
        E'                                    when ''pago_fallido'' then ''PURCHASE_CANCELED'' end',
        E'                                    when ''pago_fallido'' then ''PURCHASE_CANCELED''\n'
        || E'                                    -- flow_engine_native_intent_history (20261101000100)\n'
        || E'                                    when ''intencion'' then ''PRECHECKOUT_FORM_SUBMITTED'' end',
        'flow_engine_native_intent_history');

    -- 11. La adopcion del entrante: un primer contacto nativo ya condenado no
    -- frena (20261009000200 para el del legado). Una base sin la funcion (sin
    -- 20261001000400) no tiene nada que arreglar.
    if to_regprocedure(c_adoption) is not null then
        perform pg_temp.flow_engine_native_intent_patch(c_adoption,
            E'                             -- portable_adoption_stopped_first_contact: end\n',
            E'                             -- portable_adoption_stopped_first_contact: end\n'
            || E'                             -- flow_engine_native_intent_adoption: begin\n'
            || E'                             -- Lo mismo con un primer contacto NATIVO del\n'
            || E'                             -- formulario (20261101000100): su intencion es la\n'
            || E'                             -- del caso (no tiene binding del piloto).\n'
            || E'                             and not (\n'
            || E'                                 action.anchor_type = ''flow_engine_event''\n'
            || E'                                 and action.status in (\n'
            || E'                                     ''pending'', ''deferred'', ''retryable_failed''\n'
            || E'                                 )\n'
            || E'                                 and recovery.context ->> ''flow_engine_trigger'' = ''intencion''\n'
            || E'                                 and exists (\n'
            || E'                                     select 1\n'
            || E'                                     from public.purchase_intents stopped_native_intent\n'
            || E'                                     where stopped_native_intent.id = recovery.hotmart_purchase_intent_id\n'
            || E'                                       and (\n'
            || E'                                           coalesce(stopped_native_intent.current_classification, '''') in (\n'
            || E'                                               ''confirmed_abandonment'',\n'
            || E'                                               ''payment_failure_supported''\n'
            || E'                                           )\n'
            || E'                                           or public._portable_precheckout_stop_reason(\n'
            || E'                                               stopped_native_intent.id, recovery.contact_id\n'
            || E'                                           ) in (\n'
            || E'                                               ''intent_purchased'',\n'
            || E'                                               ''intent_purchase_ambiguous'',\n'
            || E'                                               ''purchase_by_identity''\n'
            || E'                                           )\n'
            || E'                                       )\n'
            || E'                                 )\n'
            || E'                             )\n'
            || E'                             -- flow_engine_native_intent_adoption: end\n',
            'flow_engine_native_intent_adoption: begin');
    else
        raise notice 'flow_engine_native_intent_adoption: % no existe en esta base; no se cambio', c_adoption;
    end if;
end;
$patch$;

-- ===========================================================================
-- Bloque 10. Los permisos.
-- ===========================================================================

do $acl$
declare
    v_role text;
    v_function text;
begin
    foreach v_function in array array[
        'public._flow_engine_native_intent_audience(uuid,uuid,uuid,text,text,text)',
        'public._flow_engine_native_intent_unclaimed(text)',
        'public._flow_engine_native_intent_stop_reason(uuid)',
        'public.admit_and_route_portable_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,integer)'
    ] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    revoke all on table public.flow_engine_native_intent_admissions from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on table public.flow_engine_native_intent_admissions from %I', v_role);
    end loop;
    -- La entrada y el wrapper de la reevaluacion: solo service_role.
    if exists (select 1 from pg_roles r where r.rolname = 'service_role') then
        grant execute on function public.admit_and_route_portable_lead_precheckout_v1(
            text,text,integer,text,jsonb,jsonb,text,integer) to service_role;
        grant execute on function public._flow_engine_native_intent_stop_reason(uuid) to service_role;
    end if;
end;
$acl$;

-- ===========================================================================
-- Bloque 1. La fila del recorrido. Va al final, justo antes del commit: el
-- ACCESS EXCLUSIVE sobre flow_engine_native_runs (valida sus filas) se retiene
-- lo menos posible (R9).
-- ===========================================================================

alter table public.flow_engine_native_runs
    drop constraint flow_engine_native_runs_trigger_event_check,
    add constraint flow_engine_native_runs_trigger_event_check
        check (trigger_event in ('compra', 'carrito', 'pago_fallido', 'intencion')),
    drop constraint flow_engine_native_runs_audience_by_trigger,
    add constraint flow_engine_native_runs_audience_by_trigger check (
        (trigger_event in ('carrito', 'pago_fallido', 'intencion')
         and audience_purchase_intent_id is not null and audience_precheckout_submission_id is not null)
        or (trigger_event not in ('carrito', 'pago_fallido', 'intencion')
            and audience_purchase_intent_id is null and audience_precheckout_submission_id is null));

commit;
