-- flow-engine-motor-carrito-v1: el camino del motor para el carrito (la T2.3b
-- del motor de flujos). Contrato: docs/contracts/flow-engine-v1.md (manda sobre
-- el diseno). Diseno: «el motor de flujos» (2026-10-16), §2.2 a §2.9,
-- principios 1, 2, 4, 6, 7, 8, 10, 11, 12 y 13. Especificacion: «T2.3: el
-- motor en la base» (2026-10-21), parte T2.3b, con sus decisiones 1 a 8.
--
-- Nace apagada: ninguna ruta esta en motor y ninguna funcion de esta migracion
-- planifica ni manda nada mientras no la haya. Sin rutas en motor, el legado se
-- comporta como despues de 20261021000200. Lo que trae, por bloques:
--
-- 1. Los helpers, la capacidad del motor (_flow_engine_route_kinds = {carrito}),
--    la guarda de la T2.3a con el carrito en motor (solo pausado y apagado,
--    como en legado: un A2 o un Z del legado en curso no se rechaza hasta
--    vencer), crear una ruta exige el flujo prendido (decision 8,
--    flow_route_flow_off) y el disparador del tope por persona (decision 4):
--    con una ruta del tenant en motor, no se carga ni se cambia un renglon de
--    pilot_proactive_contact_caps.
-- 2. La admision del motor, admit_flow_engine_hotmart_v1, con la subtransaccion
--    y la caida al legado con divergencia, y la delegacion en el envoltorio de
--    hoy (plan_lancemos_pilot_cart_recovery): con la ruta del carrito en motor,
--    el carrito lo admite el motor. El caso del motor nace con policy_key
--    flujo:<flow_id> y la politica compilada de N pasos, asi que A2 y Z los
--    inserta el mecanismo de hoy (_finalize_followup_delivery_attempt) con las
--    esperas de la version fijada. La delegacion va POR CASO (un renglon en
--    flow_engine_runs): lo que arranco el legado termina en el legado.
-- 3. La autorizacion del motor (_flow_engine_authorize_start) y su arranque,
--    mark_flow_engine_request_started_v1 (request_started, rechazado,
--    terminado); la delegacion en mark_lancemos_pilot_request_started para los
--    casos del motor (la imagen de hoy arranca los tres toques por ahi); y
--    mark_followup_request_started acepta tambien la autorizacion del motor.
-- 4. El contexto del paso, flow_engine_execution_context_v1 (la plantilla, el
--    idioma y las variables del paso en la version fijada), y el apagado en
--    reevaluate_followup_action: un caso del motor cuyo flujo se apago despues
--    de arrancar se cancela con flujo_apagado.
--
-- Los reemplazos van sobre la definicion VIGENTE de cada funcion, con el
-- metodo de 20261007000100 y 20261021000200: un texto exacto que tiene que
-- aparecer una sola vez, la marca que no puede estar, el duenio que es quien
-- aplica, y la marca presente despues. Solo se inserta texto: la definicion
-- vigente es la de ahora sin el bloque. Las vigentes:
--   plan_lancemos_pilot_cart_recovery ...... 20260930000300 + 20261021000200
--   mark_lancemos_pilot_request_started .... 20261001000100 + 20261021000200
--   mark_followup_request_started .......... 20260810000300
--   reevaluate_followup_action ............. 20260805000300 + 20261019000300,
--                                            20261019000400, 20261019000600,
--                                            20261019000800
--   operator_create_flow_route_v1 .......... 20261021000200
--   _flow_engine_legacy_guard .............. 20261021000200
-- _flow_engine_route_kinds se reemplaza entera (no tiene bloques).
--
-- Los motivos para la imagen de hoy del bridge (la delegacion en los
-- envoltorios) van sin ':' ni digitos: el bridge descarta un motivo con esa
-- forma (supabase.py:181 y :228-255). Por el envoltorio, un rechazo es un
-- raise, asi que la fila de rechazo y la divergencia que termina en un
-- rechazo se deshacen con el (no hay transacciones autonomas); las persiste el
-- camino de la T2.4, que llama a la admision y al arranque directo.
--
-- Lo que no hace: no crea roles, no cambia el ACL del esquema, no toca filas
-- que existan, no agrega tablas ni claves foraneas. Correrla dos veces aborta
-- con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe; 55000 un estado que no lo
-- permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.flow_engine_routes') is null then
        raise exception using errcode = '55000', message = 'flow_engine_routes_missing';
    end if;
    if to_regprocedure('public.admit_flow_engine_hotmart_v1(uuid,uuid,text,text,text,timestamptz,bigint,bigint,text,text,integer,text,integer,boolean)') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_motor_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- Bloque 1. Los helpers, la capacidad, la guarda, crear la ruta y el tope por
-- persona.
-- ===========================================================================

-- La capacidad del motor de esta version: el carrito (la regla 1 de mover a
-- motor, contrato 3.2).
create or replace function public._flow_engine_route_kinds()
returns text[]
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select array['carrito']::text[]
$function$;

-- Los envios del unico camino de una definicion, en orden: la plantilla, el
-- idioma, las variables y el boton del n-esimo.
create function public._flow_engine_sends(p_definition jsonb)
returns table (ordinal integer, plantilla text, idioma text, variables jsonb, boton jsonb)
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select (row_number() over (order by step.step_index))::integer,
           step.value ->> 'plantilla',
           step.value ->> 'idioma',
           step.value -> 'variables',
           step.value -> 'boton'
      from jsonb_array_elements(case when jsonb_typeof(p_definition -> 'pasos') = 'array'
                                     then p_definition -> 'pasos' else '[]'::jsonb end)
           with ordinality as step(value, step_index)
     where jsonb_typeof(step.value) = 'object' and step.value ? 'plantilla'
     order by step.step_index
$function$;

-- La cadena estatica de la evaluacion del scope del legado (20260930000300,
-- la evaluacion del piloto: tenant, cuenta, inbox, origen, producto y oferta),
-- sin el estado del runtime ni la cohorte. Null si todo coincide. El canal es
-- el del propio scope (el envoltorio pasa los del scope), asi que no se mira.
create function public._flow_engine_scope_static_reason(
    p_scope public.pilot_scope_versions, p_tenant_key text, p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint, p_offer_code text, p_external_product_id text)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when p_scope.scope_key is null then 'pilot_scope_not_published'
        when p_tenant_key is distinct from p_scope.tenant_key then 'pilot_tenant_mismatch'
        when p_chatwoot_account_id is distinct from p_scope.chatwoot_account_id then 'pilot_chatwoot_account_mismatch'
        when p_chatwoot_inbox_id is distinct from p_scope.chatwoot_inbox_id then 'pilot_chatwoot_inbox_mismatch'
        when p_scope.source is distinct from 'hotmart'
             or ('PURCHASE_OUT_OF_SHOPPING_CART' is distinct from p_scope.source_event_type
                 and not ('PURCHASE_OUT_OF_SHOPPING_CART' = any(coalesce(p_scope.additional_source_event_types, '{}'::text[]))))
            then 'pilot_source_event_mismatch'
        when p_external_product_id is distinct from p_scope.external_product_id then 'pilot_product_mismatch'
        when p_offer_code is distinct from p_scope.offer_code
             and not coalesce(p_offer_code = any(coalesce(p_scope.additional_offer_codes, '{}'::text[])), false)
            then 'pilot_offer_mismatch'
    end
$function$;

-- La audiencia con consentimiento del carrito: la intencion con la que la
-- admision correlaciono ESTE evento, verificada con el mismo helper que usa el
-- envoltorio (_lancemos_pilot_audience_intent). Solo la usa la admision del
-- motor; el envoltorio de hoy no se toca.
create function public._portable_cart_audience_checks(
    p_scope_key text, p_scope_version integer, p_webhook_event_id uuid, p_contact_id uuid,
    p_offer_code text, p_external_user_id text)
returns table (purchase_intent_id uuid, precheckout_submission_id uuid, reason_code text)
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select audience.purchase_intent_id, audience.precheckout_submission_id, audience.reason_code
      from public._lancemos_pilot_audience_intent(
          p_scope_key,
          p_scope_version,
          p_contact_id,
          p_offer_code,
          (select correlation.purchase_intent_id
             from public.hotmart_purchase_intent_correlations correlation
            where correlation.webhook_event_id = p_webhook_event_id
              and correlation.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
              and correlation.outcome = 'resolved'
            limit 1),
          p_external_user_id) audience
$function$;

-- Un recorrido del motor terminado por el apagado: su flujo esta apagado, o se
-- apago despues de que arranco (aunque despues se haya vuelto a prender).
-- security definer: la llama reevaluate_followup_action, que es security
-- invoker y la corre service_role, que no lee las tablas del motor.
create function public._flow_engine_run_turned_off(p_recovery_case_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select exists (
        select 1
          from public.flow_engine_runs run
          join public.flow_controls control on control.flow_id = run.flow_id
         where run.recovery_case_id = p_recovery_case_id
           and (control.state = 'apagado'
                or exists (
                    select 1 from public.flow_change_events event
                     where event.flow_id = run.flow_id
                       and event.event_type = 'flow_control_changed'
                       and event.data ->> 'to_state' = 'apagado'
                       and event.created_at >= run.started_at)))
$function$;

-- El tope por persona y el motor no conviven en la v1 (principio 12, decision
-- 4): con una ruta del tenant en motor, no se carga ni se cambia un renglon.
-- Mover a motor ya exige que no haya renglon (flow_route_person_cap_present).
create function public.reject_proactive_contact_cap_with_engine_route()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if exists (
        select 1
          from public.flow_engine_routes route
          join public.flow_controls control on control.flow_id = route.flow_id
         where route.executor = 'motor'
           and control.tenant_ref = new.tenant_key
    ) then
        raise exception using errcode = '55000', message = 'pilot_proactive_contact_cap_with_engine_route';
    end if;
    return new;
end;
$function$;

create trigger pilot_proactive_contact_caps_no_engine_route
before insert or update on public.pilot_proactive_contact_caps
for each row execute function public.reject_proactive_contact_cap_with_engine_route();

-- Los reemplazos del bloque 1, sobre la definicion vigente.
do $patch_routes$
declare
    v_signatures text[] := array[
        'public._flow_engine_legacy_guard(text,text)',
        'public.operator_create_flow_route_v1(jsonb,text,text,text,text,text,text)'
    ];
    v_anchors text[] := array[
        E'    if v_route.executor = ''motor'' then\n',
        E'    insert into public.flow_engine_routes (flow_id, executor, legacy_kind, legacy_scope_key, generation,\n'
    ];
    v_inserts text[] := array[
        E'    -- flow_engine_motor_guard: begin\n'
        || E'    -- La T2.3b (20261022000100): con el carrito en motor, la guarda es la\n'
        || E'    -- misma que con la ruta en legado (pausado y apagado). Lo que la\n'
        || E'    -- planifica o la arranca por el motor ya paso por la delegacion; lo que\n'
        || E'    -- llega aca es del legado en curso, y termina en el legado.\n'
        || E'    if v_route.executor = ''motor''\n'
        || E'       and v_route.legacy_kind = any(public._flow_engine_route_kinds()) then\n'
        || E'        return case v_state\n'
        || E'            when ''pausado'' then ''flujo_pausado''\n'
        || E'            when ''apagado'' then ''flujo_apagado''\n'
        || E'        end;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_motor_guard: end\n',
        E'    -- flow_engine_route_flow_on: begin\n'
        || E'    -- La T2.3b (20261022000100, decision 8): la ruta se crea con el flujo\n'
        || E'    -- prendido. Un flujo recien registrado nace apagado, y una ruta suya en\n'
        || E'    -- legado frenaria el carrito real con la guarda.\n'
        || E'    if v_control.state is distinct from ''prendido'' then\n'
        || E'        return jsonb_build_object(''ok'', false, ''reason_code'', ''flow_route_flow_off'');\n'
        || E'    end if;\n'
        || E'    -- flow_engine_route_flow_on: end\n'
    ];
    v_markers text[] := array['flow_engine_motor_guard: begin', 'flow_engine_route_flow_on: begin'];
    v_definition text;
    i integer;
begin
    for i in 1 .. array_length(v_signatures, 1) loop
        if to_regprocedure(v_signatures[i]) is null then
            raise exception using errcode = '55000', message = 'flow_engine_motor_function_missing',
                detail = v_signatures[i];
        end if;
        if (select p.proowner from pg_proc p where p.oid = v_signatures[i]::regprocedure)
           is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
            raise exception using errcode = '55000', message = 'flow_engine_motor_owner_mismatch',
                detail = v_signatures[i];
        end if;
        v_definition := pg_get_functiondef(v_signatures[i]::regprocedure);
        if length(v_definition) - length(replace(v_definition, v_anchors[i], '')) <> length(v_anchors[i])
           or position(v_markers[i] in v_definition) > 0 then
            raise exception using errcode = '55000', message = 'unexpected_flow_engine_motor_definition',
                detail = v_signatures[i];
        end if;
        execute replace(v_definition, v_anchors[i], v_inserts[i] || v_anchors[i]);
        if position(v_markers[i] in pg_get_functiondef(v_signatures[i]::regprocedure)) = 0 then
            raise exception using errcode = '55000', message = 'flow_engine_motor_definition_not_replaced',
                detail = v_signatures[i];
        end if;
    end loop;
end;
$patch_routes$;

do $acl_routes$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._flow_engine_sends(jsonb)',
        'public._flow_engine_scope_static_reason(public.pilot_scope_versions,text,bigint,bigint,text,text)',
        'public._portable_cart_audience_checks(text,integer,uuid,uuid,text,text)',
        'public._flow_engine_run_turned_off(uuid)',
        'public.reject_proactive_contact_cap_with_engine_route()'
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
    -- La reevaluacion (security invoker, la corre service_role) la llama.
    if to_regrole('service_role') is not null then
        grant execute on function public._flow_engine_run_turned_off(uuid) to service_role;
    end if;
end;
$acl_routes$;

-- ===========================================================================
-- Bloque 2. La admision del motor y la delegacion en el envoltorio.
-- ===========================================================================

-- Un motivo con la forma de las tablas del motor, o el de repuesto.
create function public._flow_engine_reason(p_reason text, p_fallback text)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select case when coalesce(p_reason ~ '^[a-z0-9_:]{1,80}$', false) then p_reason else p_fallback end
$function$;

-- Un disparo que el motor no arranco, o un arranque rechazado: suma count.
create function public._flow_engine_record_start_rejection(
    p_flow_id uuid, p_version integer, p_source_ref uuid, p_phase text, p_reason text)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.flow_engine_start_rejections as rejection (flow_id, version, source_ref, phase, reason)
    values (p_flow_id, p_version, p_source_ref, p_phase,
            public._flow_engine_reason(p_reason, 'motivo_invalido'))
    on conflict (flow_id, source_ref, reason) do update
       set count = rejection.count + 1, last_at = clock_timestamp(), version = excluded.version
$function$;

-- Una caida al legado: una fila por flujo, clase y motivo; first es la primera.
create function public._flow_engine_record_divergence(
    p_kind text, p_flow_id uuid, p_version integer, p_reason text, p_sqlstate text, p_source_ref uuid)
returns table (divergence_id uuid, divergence_first boolean)
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.flow_engine_divergences as divergence
        (kind, flow_id, version, reason, sqlstate, first_source_ref, last_source_ref)
    values (p_kind, p_flow_id, p_version, public._flow_engine_reason(p_reason, p_kind || ':motivo_invalido'),
            case when coalesce(p_sqlstate ~ '^[0-9A-Z]{5}$', false) then p_sqlstate end,
            p_source_ref, p_source_ref)
    on conflict on constraint flow_engine_divergences_one_row do update
       set count = divergence.count + 1, last_at = clock_timestamp(),
           last_source_ref = excluded.last_source_ref, version = excluded.version,
           sqlstate = excluded.sqlstate
    returning divergence.id, (divergence.xmax = 0)
$function$;

-- El envoltorio de hoy, protegido: con la marca de caida puesta (el envoltorio
-- no vuelve a delegar ese evento) y su rechazo (55000) atrapado. Cualquier
-- otro error sigue de largo, como hoy.
create function public._flow_engine_plan_legacy_cart(
    p_webhook_event_id uuid, p_contact_id uuid, p_external_product_id text, p_product_name text,
    p_offer_code text, p_abandoned_at timestamptz, p_chatwoot_account_id bigint, p_chatwoot_inbox_id bigint,
    p_destination_phone text, p_legacy_scope_key text, p_legacy_scope_version integer,
    p_legacy_policy_key text, p_legacy_policy_version integer)
returns table (ok boolean, reason text, recovery_case_id uuid, followup_sequence_id uuid,
               scheduled_action_id uuid, created boolean)
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_plan record;
    v_message text;
    v_detail text;
begin
    perform set_config('flow_engine.legacy_fallback_event', p_webhook_event_id::text, true);
    begin
        select legacy.recovery_case_id as case_id, legacy.followup_sequence_id as sequence_id,
               legacy.scheduled_action_id as action_id, legacy.created as was_created
          into v_plan
          from public.plan_lancemos_pilot_cart_recovery(
              p_webhook_event_id, p_contact_id, p_external_product_id, p_product_name, p_offer_code,
              p_legacy_policy_key, p_legacy_policy_version, p_abandoned_at, p_chatwoot_account_id,
              p_chatwoot_inbox_id, p_destination_phone, p_legacy_scope_key, p_legacy_scope_version) legacy;
    exception when sqlstate '55000' then
        get stacked diagnostics v_message = message_text, v_detail = pg_exception_detail;
        return query select false,
            case when coalesce(v_message ~ '^[a-z][a-z0-9_]{0,79}$', false) then
                v_message || case when coalesce(v_detail ~ '^[a-z0-9_]{1,80}$', false)
                                  then ':' || v_detail else '' end
            else 'legado_rechazo_sin_motivo' end,
            null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end;
    return query select true, null::text, v_plan.case_id, v_plan.sequence_id, v_plan.action_id, v_plan.was_created;
end;
$function$;

-- Si la imagen de hoy del bridge puede despachar una version (§5 del plan):
-- cada envio (plantilla, idioma, variables y boton) igual al de la primera
-- version del flujo, la que traduce el manifiesto, y la misma cantidad.
create function public._flow_engine_legacy_dispatch_ok(p_flow_id uuid, p_version integer)
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select coalesce((
        select jsonb_agg(jsonb_build_array(s.plantilla, s.idioma, s.variables, s.boton) order by s.ordinal)
          from public.flow_definitions d, public._flow_engine_sends(d.definition) s
         where d.flow_id = p_flow_id and d.version = p_version
    ) = (
        select jsonb_agg(jsonb_build_array(s.plantilla, s.idioma, s.variables, s.boton) order by s.ordinal)
          from public.flow_definitions d, public._flow_engine_sends(d.definition) s
         where d.flow_id = p_flow_id
           and d.version = (select min(first.version) from public.flow_definitions first
                             where first.flow_id = p_flow_id)
    ), false)
$function$;

-- El camino del motor de la admision, sin la caida. Devuelve motor_arranco,
-- motor_no_arranca o legado (sin ruta en motor, o la repeticion de un evento
-- que ya planifico el legado: decision 6). Lo que lo hace caer al legado lo
-- levanta como 55000 flow_engine_divergence con detail '<clase>:<motivo>', o
-- como el error que sea; la admision lo atrapa. Los locks, en el orden de
-- §2.4: (1) la ruta y (2) el control del flujo FOR SHARE, (3) el control del
-- scope FOR UPDATE, y despues el evento y el contacto, como el planificador.
create function public._flow_engine_admit_cart_motor(
    p_webhook_event_id uuid, p_contact_id uuid, p_external_product_id text, p_product_name text,
    p_offer_code text, p_abandoned_at timestamptz, p_chatwoot_account_id bigint, p_chatwoot_inbox_id bigint,
    p_destination_phone text, p_legacy_scope_key text)
returns table (outcome text, reason text, flow_id uuid, flow_version integer, recovery_case_id uuid,
               followup_sequence_id uuid, scheduled_action_id uuid, created boolean)
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_route public.flow_engine_routes%rowtype;
    v_control public.flow_controls%rowtype;
    v_definition public.flow_definitions%rowtype;
    v_event_binding public.commercial_ally_hotmart_event_bindings%rowtype;
    v_runtime public.pilot_runtime_controls%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
    v_static text;
    v_audience record;
    v_compiled public.followup_policy_versions%rowtype;
    v_stored public.followup_policy_versions%rowtype;
    v_policy_key text;
    v_event public.webhook_events%rowtype;
    v_event_case uuid;
    v_open_case uuid;
    v_open_run public.flow_engine_runs%rowtype;
    v_open_reason text;
    v_plan record;
    v_reason text;
    v_field text;
    v_message text;
    v_sequence_id uuid;
    v_action_id uuid;
begin
    -- (1) La ruta.
    select route.* into v_route
      from public.flow_engine_routes route
     where route.legacy_scope_key = p_legacy_scope_key and route.legacy_kind = 'carrito'
     for share;
    if not found or v_route.executor = 'legado' then
        return query select 'legado'::text, null::text, v_route.flow_id, null::integer,
            null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- (2) El control del flujo. Apagado no arranca y no deja fila; pausado
    -- deja la suya.
    select control.* into strict v_control
      from public.flow_controls control where control.flow_id = v_route.flow_id
     for share;
    if v_control.state = 'apagado' then
        return query select 'motor_no_arranca'::text, 'flujo_apagado'::text, v_control.flow_id,
            v_control.active_version, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    if v_control.state = 'pausado' then
        perform public._flow_engine_record_start_rejection(
            v_control.flow_id, v_control.active_version, p_webhook_event_id, 'admision', 'flujo_pausado');
        return query select 'motor_no_arranca'::text, 'flujo_pausado'::text, v_control.flow_id,
            v_control.active_version, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- La version activa y lo que el motor de esta version sabe ejecutar.
    select definition_row.* into v_definition
      from public.flow_definitions definition_row
     where definition_row.flow_id = v_control.flow_id and definition_row.version = v_control.active_version;
    if not found then
        raise exception using errcode = '55000', message = 'flow_engine_divergence', detail = 'ruta:sin_version_activa';
    end if;
    if not public._flow_engine_kind_matches('carrito', v_definition.definition) then
        raise exception using errcode = '55000', message = 'flow_engine_divergence', detail = 'ruta:disparador_distinto';
    end if;
    -- Decision 5: la repeticion despues de un recorrido no la ejecuta la v1.
    if v_definition.definition #> '{topes,repeticion,otra_despues_de}' is distinct from '"0m"'::jsonb then
        raise exception using errcode = '55000', message = 'flow_engine_divergence', detail = 'ruta:repeticion_no_soportada';
    end if;
    -- La bolsa es el scope del legado: el tope compartido se cuenta por
    -- scope_key de los dos lados, y una bolsa sin control rechazaria cada
    -- arranque (flow_engine_pool_unknown) hasta vencer.
    if coalesce(v_definition.definition #>> '{topes,bolsa}', p_legacy_scope_key) is distinct from p_legacy_scope_key then
        raise exception using errcode = '55000', message = 'flow_engine_divergence', detail = 'ruta:bolsa_distinta_del_scope';
    end if;

    -- El producto (principio 11): el evento es del producto del flujo.
    select event_binding.* into v_event_binding
      from public.commercial_ally_hotmart_event_bindings event_binding
     where event_binding.webhook_event_id = p_webhook_event_id;
    if not found
       or v_event_binding.tenant_ref is distinct from v_control.tenant_ref
       or v_event_binding.funnel_ref is distinct from v_control.product_ref
       or v_event_binding.hotmart_product_id is distinct from p_external_product_id then
        raise exception using errcode = '55000', message = 'flow_engine_divergence', detail = 'producto:evento_de_otro_producto';
    end if;

    -- (3) El control del scope del legado: serializa con el legado y con los
    -- cambios del operador. La version del scope es la del control (decision
    -- 3); el estado del runtime no se mira (el motor lo frena el almacen).
    select runtime.* into v_runtime
      from public.pilot_runtime_controls runtime where runtime.scope_key = p_legacy_scope_key
     for update;
    if not found then
        raise exception using errcode = '55000', message = 'flow_engine_divergence', detail = 'ruta:scope_sin_control';
    end if;
    select scope.* into v_scope
      from public.pilot_scope_versions scope
     where scope.scope_key = p_legacy_scope_key and scope.version = v_runtime.scope_version
       and scope.status = 'published';
    v_static := public._flow_engine_scope_static_reason(
        v_scope, v_control.tenant_ref, p_chatwoot_account_id, p_chatwoot_inbox_id, p_offer_code,
        p_external_product_id);
    -- Decision 2: lo estatico no es una divergencia; el codigo del legado.
    if v_static is not null then
        return query select 'motor_no_arranca'::text, v_static, v_control.flow_id, v_definition.version,
            null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    -- Decision 5: un scope con cohorte no lo ejecuta la v1.
    if v_scope.audience_mode is distinct from 'consented_intent' then
        raise exception using errcode = '55000', message = 'flow_engine_divergence', detail = 'ruta:audiencia_con_cohorte';
    end if;

    -- La audiencia (principio 6).
    select audience.* into v_audience
      from public._portable_cart_audience_checks(
          p_legacy_scope_key, v_scope.version, p_webhook_event_id, p_contact_id, p_offer_code,
          p_destination_phone) audience;
    if v_audience.reason_code is distinct from 'pilot_audience_allowed' then
        v_reason := case
            when v_audience.reason_code like '%prior_opt_out' then 'baja'
            when v_audience.reason_code like '%not_live' then 'ya_no_entra'
            else 'no_entra:permiso'
        end;
        perform public._flow_engine_record_start_rejection(
            v_control.flow_id, v_definition.version, p_webhook_event_id, 'admision', v_reason);
        return query select 'motor_no_arranca'::text, v_reason, v_control.flow_id, v_definition.version,
            null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- La politica compilada (principio 2): se inserta publicada, con la
    -- aprobacion de la definicion, y la que quede tiene que ser esta.
    v_policy_key := 'flujo:' || v_control.flow_id::text;
    begin
        v_compiled := public._flow_engine_compile_policy(
            v_definition.definition, v_control.flow_id, v_definition.version, 'carrito');
    exception when sqlstate '22023' then
        get stacked diagnostics v_message = message_text;
        raise exception using errcode = '55000', message = 'flow_engine_divergence',
            detail = public._flow_engine_reason(left('politica_compilada:' || v_message, 80),
                                                'politica_compilada:no_compila');
    end;
    insert into public.followup_policy_versions (
        policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
        max_automatic_messages, steps, approved_by, approved_at, published_at)
    values (
        v_policy_key, v_definition.version, 'published', v_compiled.purpose, v_compiled.timezone,
        v_compiled.business_windows, v_compiled.grace_period, v_compiled.expires_after,
        v_compiled.max_automatic_messages, v_compiled.steps, v_definition.approved_by_ref,
        v_definition.published_at, v_definition.published_at)
    on conflict (policy_key, version) do nothing;
    select stored.* into strict v_stored
      from public.followup_policy_versions stored
     where stored.policy_key = v_policy_key and stored.version = v_definition.version;
    v_field := case
        when v_stored.status is distinct from 'published' then 'status'
        when v_stored.purpose is distinct from v_compiled.purpose then 'purpose'
        when v_stored.timezone is distinct from v_compiled.timezone then 'timezone'
        when v_stored.business_windows is distinct from v_compiled.business_windows then 'business_windows'
        when v_stored.grace_period is distinct from v_compiled.grace_period then 'grace_period'
        when v_stored.expires_after is distinct from v_compiled.expires_after then 'expires_after'
        when v_stored.max_automatic_messages is distinct from v_compiled.max_automatic_messages
            then 'max_automatic_messages'
        when v_stored.steps is distinct from v_compiled.steps then 'steps'
    end;
    if v_field is not null then
        raise exception using errcode = '55000', message = 'flow_engine_divergence',
            detail = 'politica_compilada:' || v_field;
    end if;

    -- (6) La persona (principio 13), en el orden del planificador: el evento y
    -- despues el contacto.
    select event_row.* into v_event
      from public.webhook_events event_row where event_row.id = p_webhook_event_id
     for update;
    if not found or v_event.event_type is distinct from 'PURCHASE_OUT_OF_SHOPPING_CART'
       or v_event.source not in ('hotmart', 'simulator') then
        raise exception using errcode = '55000', message = 'flow_engine_divergence',
            detail = 'admision_al_legado:evento_invalido';
    end if;
    perform 1 from public.contacts contact_row where contact_row.id = p_contact_id for update;
    if not found then
        raise exception using errcode = '55000', message = 'flow_engine_divergence',
            detail = 'admision_al_legado:contacto_inexistente';
    end if;

    -- La repeticion del evento: la del motor devuelve su caso; la del legado
    -- es del legado (decision 6).
    select case_event.recovery_case_id into v_event_case
      from public.recovery_case_events case_event
     where case_event.webhook_event_id = p_webhook_event_id
     limit 1;
    if v_event_case is not null then
        if exists (select 1 from public.flow_engine_runs run
                    where run.recovery_case_id = v_event_case and run.flow_id = v_control.flow_id) then
            select sequence_row.id, action_row.id into v_sequence_id, v_action_id
              from public.followup_sequences sequence_row
              join public.scheduled_actions action_row on action_row.followup_sequence_id = sequence_row.id
             where sequence_row.recovery_case_id = v_event_case
             order by action_row.created_at asc
             limit 1;
            return query select 'motor_arranco'::text, null::text, v_control.flow_id, v_definition.version,
                v_event_case, v_sequence_id, v_action_id, false;
            return;
        end if;
        return query select 'legado'::text, null::text, v_control.flow_id, null::integer,
            null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- Un caso abierto de la persona por el mismo producto y oferta (la consulta
    -- del planificador de hoy): el mismo flujo ya esta en curso, u otro flujo
    -- no deja arrancar. No se llama al planificador: no mueve nada del caso.
    select open_case.id into v_open_case
      from public.recovery_cases open_case
     where open_case.contact_id = p_contact_id
       and open_case.source = v_event.source
       and open_case.external_product_id = p_external_product_id
       and open_case.offer_code is not distinct from p_offer_code
       and open_case.status in ('grace_period', 'active', 'paused')
     order by open_case.created_at asc
     limit 1
     for update;
    if v_open_case is not null then
        select run.* into v_open_run from public.flow_engine_runs run where run.recovery_case_id = v_open_case;
        if v_open_run.recovery_case_id is not null then
            v_open_reason := case when v_open_run.flow_id = v_control.flow_id
                                       or v_open_run.legacy_kind = 'carrito' then 'ya_en_curso'
                                  else 'no_arranca_mientras:' || coalesce(v_open_run.legacy_kind, 'otro') end;
        else
            select case sequence_row.reason
                       when 'cart_abandonment' then 'ya_en_curso'
                       when 'payment_failure' then 'no_arranca_mientras:pago_fallido'
                       when 'payment_slip' then 'no_arranca_mientras:ficha'
                       when 'precheckout_intent' then 'no_arranca_mientras:precheckout'
                       else 'no_arranca_mientras:otro'
                   end
              into v_open_reason
              from public.followup_sequences sequence_row
             where sequence_row.recovery_case_id = v_open_case
             order by sequence_row.created_at asc
             limit 1;
            v_open_reason := coalesce(v_open_reason, 'no_arranca_mientras:otro');
        end if;
        perform public._flow_engine_record_start_rejection(
            v_control.flow_id, v_definition.version, p_webhook_event_id, 'admision', v_open_reason);
        return query select 'motor_no_arranca'::text, v_open_reason, v_control.flow_id, v_definition.version,
            null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- El planificador base de hoy, con la politica del flujo.
    select plan.recovery_case_id as case_id, plan.followup_sequence_id as sequence_id,
           plan.scheduled_action_id as action_id, plan.created as was_created
      into v_plan
      from public.plan_cart_recovery_with_identity(
          p_webhook_event_id, p_contact_id, p_external_product_id, p_product_name, p_offer_code,
          v_policy_key, v_definition.version, p_abandoned_at, p_chatwoot_account_id, p_chatwoot_inbox_id,
          p_destination_phone) plan;
    if v_plan.was_created is not true then
        raise exception using errcode = '55000', message = 'flow_engine_divergence',
            detail = 'admision_al_legado:created_false';
    end if;

    -- El binding del piloto: lo leen la autorizacion, la guarda y la adopcion
    -- de la respuesta (20261001000400).
    insert into public.pilot_recovery_case_bindings (
        recovery_case_id, scope_key, scope_version, source_event_id, audience_mode,
        audience_purchase_intent_id, audience_precheckout_submission_id)
    values (
        v_plan.case_id, p_legacy_scope_key, v_runtime.scope_version, p_webhook_event_id, v_scope.audience_mode,
        v_audience.purchase_intent_id, v_audience.precheckout_submission_id);

    -- El recorrido, con la version fijada.
    insert into public.flow_engine_runs (
        recovery_case_id, flow_id, version, definition_sha256, product_ref, flow_key, pool, legacy_kind,
        legacy_scope_key, person_ref, trigger_source_ref, legacy_dispatch_ok)
    values (
        v_plan.case_id, v_control.flow_id, v_definition.version, v_definition.definition_sha256,
        v_control.product_ref, v_control.flow_key,
        coalesce(v_definition.definition #>> '{topes,bolsa}', p_legacy_scope_key), 'carrito',
        p_legacy_scope_key, p_contact_id, p_webhook_event_id,
        public._flow_engine_legacy_dispatch_ok(v_control.flow_id, v_definition.version));

    return query select 'motor_arranco'::text, null::text, v_control.flow_id, v_definition.version,
        v_plan.case_id, v_plan.sequence_id, v_plan.action_id, true;
end;
$function$;

-- La admision del motor para un carrito de Hotmart (contrato, «La admision
-- del motor»). p_destination_phone es lo que hoy va como p_external_user_id.
-- Los p_legacy_* son los del envoltorio de hoy: los usa la caida al legado.
-- outcome:
--   motor_arranco           el caso del motor (created = false en una repeticion);
--   motor_no_arranca        reason: flujo_apagado, flujo_pausado, un codigo
--                           estatico del legado, baja, ya_no_entra,
--                           no_entra:permiso, ya_en_curso o
--                           no_arranca_mientras:<flujo>;
--   legado                  sin ruta en motor (o la repeticion de un evento del
--                           legado): el envoltorio de hoy planifico;
--   legado_por_divergencia  el motor no pudo y el legado planifico; divergence_*
--                           es la fila de flow_engine_divergences;
--   legado_rechazo          el legado rechazo; reason = '<message>:<detail>'.
create function public.admit_flow_engine_hotmart_v1(
    p_webhook_event_id uuid, p_contact_id uuid, p_external_product_id text, p_product_name text,
    p_offer_code text, p_abandoned_at timestamptz, p_chatwoot_account_id bigint, p_chatwoot_inbox_id bigint,
    p_destination_phone text, p_legacy_scope_key text, p_legacy_scope_version integer,
    p_legacy_policy_key text, p_legacy_policy_version integer, p_force_legacy boolean default false)
returns table (outcome text, recovery_case_id uuid, created boolean, reason text, divergence_id uuid,
               divergence_first boolean, followup_sequence_id uuid, scheduled_action_id uuid)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_motor record;
    v_legacy record;
    v_divergence record;
    v_path text;
    v_outcome text;
    v_reason text;
    v_flow_id uuid;
    v_version integer;
    v_case_id uuid;
    v_sequence_id uuid;
    v_action_id uuid;
    v_created boolean;
    v_divergence_id uuid;
    v_divergence_first boolean;
    v_sqlstate text;
    v_message text;
    v_detail text;
    v_kind text;
    v_lock_timeout text := current_setting('lock_timeout');
begin
    if p_webhook_event_id is null or p_contact_id is null
       or p_external_product_id is null or btrim(p_external_product_id) = ''
       or p_product_name is null or btrim(p_product_name) = ''
       or p_offer_code is null or btrim(p_offer_code) = ''
       or p_abandoned_at is null
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or not coalesce(p_destination_phone ~ '^[0-9]+$', false)
       or not coalesce(p_legacy_scope_key ~ '^[a-z0-9][a-z0-9_-]{0,127}$', false)
       or p_legacy_scope_version is null or p_legacy_scope_version < 1
       or p_legacy_policy_key is null or btrim(p_legacy_policy_key) = ''
       or p_legacy_policy_version is null or p_legacy_policy_version < 1
       or p_force_legacy is null then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_admission_parameters';
    end if;
    -- El camino del motor espera un lock a lo sumo 3 s y cae al legado como
    -- transitorio. En el cuerpo y no en la firma: la lista de las funciones con
    -- lock_timeout en la firma la fija validate_flow_store.mjs. Se restaura al
    -- salir del camino del motor: el legado espera como hoy.
    perform set_config('lock_timeout', '3s', true);

    if p_force_legacy then
        -- El bridge la pide despues de no poder admitir por el motor: la ruta
        -- sin lock, una divergencia transitoria y el legado.
        select route.flow_id into v_flow_id
          from public.flow_engine_routes route
         where route.legacy_scope_key = p_legacy_scope_key and route.legacy_kind = 'carrito';
        if v_flow_id is not null then
            select d.divergence_id, d.divergence_first into v_divergence_id, v_divergence_first
              from public._flow_engine_record_divergence(
                  'transitorio', v_flow_id, null, 'transitorio:forzado_al_legado', null, p_webhook_event_id) d;
            v_path := 'divergencia';
        else
            v_path := 'legado';
        end if;
    else
        begin
            select motor.* into v_motor
              from public._flow_engine_admit_cart_motor(
                  p_webhook_event_id, p_contact_id, p_external_product_id, p_product_name, p_offer_code,
                  p_abandoned_at, p_chatwoot_account_id, p_chatwoot_inbox_id, p_destination_phone,
                  p_legacy_scope_key) motor;
            if v_motor.outcome = 'legado' then
                v_path := 'legado';
            else
                v_path := 'motor';
                v_outcome := v_motor.outcome;
                v_reason := v_motor.reason;
                v_case_id := v_motor.recovery_case_id;
                v_sequence_id := v_motor.followup_sequence_id;
                v_action_id := v_motor.scheduled_action_id;
                v_created := v_motor.created;
            end if;
        exception when others then
            get stacked diagnostics v_sqlstate = returned_sqlstate, v_message = message_text,
                                    v_detail = pg_exception_detail;
            if v_message = 'flow_engine_divergence'
               and split_part(coalesce(v_detail, ''), ':', 1) in
                   ('ruta', 'producto', 'politica_compilada', 'admision_al_legado') then
                v_kind := split_part(v_detail, ':', 1);
                v_reason := v_detail;
            elsif v_sqlstate in ('40P01', '55P03', '40001') then
                v_kind := 'transitorio';
                v_reason := 'transitorio:sqlstate_' || lower(v_sqlstate);
            else
                v_kind := 'admision_al_legado';
                v_reason := 'admision_al_legado:sqlstate_' || lower(v_sqlstate);
            end if;
            -- El flujo de la ruta, sin lock (la subtransaccion ya se deshizo).
            select route.flow_id into v_flow_id
              from public.flow_engine_routes route
             where route.legacy_scope_key = p_legacy_scope_key and route.legacy_kind = 'carrito';
            if v_flow_id is not null then
                select control.active_version into v_version
                  from public.flow_controls control where control.flow_id = v_flow_id;
                select d.divergence_id, d.divergence_first into v_divergence_id, v_divergence_first
                  from public._flow_engine_record_divergence(
                      v_kind, v_flow_id, v_version, v_reason, v_sqlstate, p_webhook_event_id) d;
            end if;
            v_reason := null;
            v_path := 'divergencia';
        end;
    end if;

    perform set_config('lock_timeout', v_lock_timeout, true);
    if v_path in ('legado', 'divergencia') then
        select legacy.* into v_legacy
          from public._flow_engine_plan_legacy_cart(
              p_webhook_event_id, p_contact_id, p_external_product_id, p_product_name, p_offer_code,
              p_abandoned_at, p_chatwoot_account_id, p_chatwoot_inbox_id, p_destination_phone,
              p_legacy_scope_key, p_legacy_scope_version, p_legacy_policy_key, p_legacy_policy_version) legacy;
        if v_legacy.ok then
            v_outcome := case when v_path = 'legado' then 'legado' else 'legado_por_divergencia' end;
            v_case_id := v_legacy.recovery_case_id;
            v_sequence_id := v_legacy.followup_sequence_id;
            v_action_id := v_legacy.scheduled_action_id;
            v_created := v_legacy.created;
        else
            v_outcome := 'legado_rechazo';
            v_reason := v_legacy.reason;
        end if;
    end if;

    return query select v_outcome, v_case_id, v_created, v_reason, v_divergence_id, v_divergence_first,
        v_sequence_id, v_action_id;
end;
$function$;

-- La delegacion en el envoltorio de hoy, sobre su definicion vigente.
do $patch_admission$
declare
    c_signature constant text :=
        'public.plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)';
    c_anchor constant text := E'    -- flow_engine_legacy_guard: begin\n';
    c_marker constant text := 'flow_engine_motor_delegation: begin';
    v_insert text;
    v_definition text;
begin
    v_insert :=
        E'    -- flow_engine_motor_delegation: begin\n'
        || E'    -- La T2.3b (20261022000100): con la ruta del carrito en motor, el\n'
        || E'    -- carrito lo admite el motor (admit_flow_engine_hotmart_v1), salvo el\n'
        || E'    -- evento que la admision esta mandando al legado (la marca de caida).\n'
        || E'    -- Los motivos van sin dos puntos ni digitos: el bridge de hoy descarta\n'
        || E'    -- un motivo con esa forma.\n'
        || E'    declare\n'
        || E'        v_flow_engine_admission record;\n'
        || E'    begin\n'
        || E'        if exists (\n'
        || E'               select 1 from public.flow_engine_routes engine_route\n'
        || E'                where engine_route.legacy_scope_key = p_scope_key\n'
        || E'                  and engine_route.legacy_kind = ''carrito''\n'
        || E'                  and engine_route.executor = ''motor'')\n'
        || E'           and nullif(btrim(p_product_name), '''') is not null\n'
        || E'           and nullif(btrim(p_offer_code), '''') is not null\n'
        || E'           and coalesce(p_external_user_id ~ ''^[0-9]+$'', false)\n'
        || E'           and coalesce(current_setting(''flow_engine.legacy_fallback_event'', true), '''')\n'
        || E'               is distinct from p_webhook_event_id::text then\n'
        || E'            select admission.outcome as engine_outcome, admission.reason as engine_reason,\n'
        || E'                   admission.recovery_case_id as engine_case_id,\n'
        || E'                   admission.followup_sequence_id as engine_sequence_id,\n'
        || E'                   admission.scheduled_action_id as engine_action_id,\n'
        || E'                   admission.created as engine_created\n'
        || E'              into v_flow_engine_admission\n'
        || E'              from public.admit_flow_engine_hotmart_v1(\n'
        || E'                  p_webhook_event_id, p_contact_id, p_external_product_id, p_product_name,\n'
        || E'                  p_offer_code, p_abandoned_at, p_chatwoot_account_id, p_chatwoot_inbox_id,\n'
        || E'                  p_external_user_id, p_scope_key, p_scope_version, p_policy_key,\n'
        || E'                  p_policy_version, false) admission;\n'
        || E'            if v_flow_engine_admission.engine_outcome in\n'
        || E'               (''motor_arranco'', ''legado_por_divergencia'', ''legado'') then\n'
        || E'                return query select v_flow_engine_admission.engine_case_id,\n'
        || E'                    v_flow_engine_admission.engine_sequence_id,\n'
        || E'                    v_flow_engine_admission.engine_action_id,\n'
        || E'                    v_flow_engine_admission.engine_created;\n'
        || E'                return;\n'
        || E'            elsif v_flow_engine_admission.engine_outcome = ''motor_no_arranca'' then\n'
        || E'                raise exception using\n'
        || E'                    errcode = ''55000'',\n'
        || E'                    message = ''pilot_scope_rejected'',\n'
        || E'                    detail = translate(v_flow_engine_admission.engine_reason, '':0123456789'', ''_'');\n'
        || E'            else\n'
        || E'                raise exception using\n'
        || E'                    errcode = ''55000'',\n'
        || E'                    message = split_part(v_flow_engine_admission.engine_reason, '':'', 1),\n'
        || E'                    detail = nullif(substr(v_flow_engine_admission.engine_reason,\n'
        || E'                        length(split_part(v_flow_engine_admission.engine_reason, '':'', 1)) + 2), '''');\n'
        || E'            end if;\n'
        || E'        end if;\n'
        || E'    end;\n'
        || E'    -- flow_engine_motor_delegation: end\n'
        || E'\n';
    if to_regprocedure(c_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_motor_function_missing', detail = c_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_motor_owner_mismatch', detail = c_signature;
    end if;
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, c_anchor, '')) <> length(c_anchor)
       or position(c_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_motor_definition',
            detail = c_signature;
    end if;
    execute replace(v_definition, c_anchor, v_insert || c_anchor);
    if position(c_marker in pg_get_functiondef(c_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_motor_definition_not_replaced',
            detail = c_signature;
    end if;
end;
$patch_admission$;

do $acl_admission$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._flow_engine_reason(text,text)',
        'public._flow_engine_record_start_rejection(uuid,integer,uuid,text,text)',
        'public._flow_engine_record_divergence(text,uuid,integer,text,text,uuid)',
        'public._flow_engine_plan_legacy_cart(uuid,uuid,text,text,text,timestamptz,bigint,bigint,text,text,integer,text,integer)',
        'public._flow_engine_legacy_dispatch_ok(uuid,integer)',
        'public._flow_engine_admit_cart_motor(uuid,uuid,text,text,text,timestamptz,bigint,bigint,text,text)',
        'public.admit_flow_engine_hotmart_v1(uuid,uuid,text,text,text,timestamptz,bigint,bigint,text,text,integer,text,integer,boolean)'
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
        grant execute on function public.admit_flow_engine_hotmart_v1(
            uuid, uuid, text, text, text, timestamptz, bigint, bigint, text, text, integer, text, integer, boolean)
            to service_role;
    end if;
end;
$acl_admission$;

-- ===========================================================================
-- Bloque 3. La autorizacion y el arranque del motor.
-- ===========================================================================

-- La autorizacion de un arranque del motor (principios 4, 6, 8 y 12). outcome
-- authorized (con su fila en flow_engine_request_authorizations), rechazado
-- (no escribe nada) o terminado (el flujo se apago despues de arrancar). Los
-- locks: (2) el control del flujo FOR SHARE y (3) el control del scope de la
-- bolsa FOR UPDATE, el lock real de la bolsa, como el legado. Los topes son
-- los de la definicion fijada, contados sobre la bolsa por los dos caminos.
-- p_require_legacy_dispatch: la imagen de hoy del bridge solo despacha una
-- version cuyos envios son los de la primera (flow_engine_needs_engine_image).
create function public._flow_engine_authorize_start(
    p_action_id uuid, p_attempt_id uuid, p_now timestamptz, p_require_legacy_dispatch boolean)
returns table (outcome text, reason text, authorization_id uuid, flow_generation bigint, replayed boolean)
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_action public.scheduled_actions%rowtype;
    v_case public.recovery_cases%rowtype;
    v_run public.flow_engine_runs%rowtype;
    v_existing public.flow_engine_request_authorizations%rowtype;
    v_control public.flow_controls%rowtype;
    v_pool public.pilot_runtime_controls%rowtype;
    v_binding public.pilot_recovery_case_bindings%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_identity public.channel_identities%rowtype;
    v_policy public.followup_policy_versions%rowtype;
    v_definition jsonb;
    v_audience record;
    v_clock timestamptz;
    v_local_date date;
    v_total integer;
    v_daily integer;
    v_id uuid;
begin
    select action_row.* into v_action from public.scheduled_actions action_row where action_row.id = p_action_id;
    select run.* into v_run from public.flow_engine_runs run where run.recovery_case_id = v_action.recovery_case_id;
    if v_action.id is null or v_run.recovery_case_id is null then
        raise exception using errcode = '55000', message = 'flow_engine_run_not_found';
    end if;
    select case_row.* into strict v_case from public.recovery_cases case_row where case_row.id = v_run.recovery_case_id;
    if v_action.policy_key is distinct from 'flujo:' || v_run.flow_id::text
       or v_action.policy_version is distinct from v_run.version then
        return query select 'rechazado'::text, 'pilot_attempt_mismatch'::text, null::uuid, null::bigint, false;
        return;
    end if;

    -- La repeticion de un arranque ya autorizado.
    select engine_row.* into v_existing
      from public.flow_engine_request_authorizations engine_row where engine_row.attempt_id = p_attempt_id;
    if found then
        if v_existing.action_id <> p_action_id then
            return query select 'rechazado'::text, 'pilot_attempt_mismatch'::text, null::uuid, null::bigint, false;
        else
            return query select 'authorized'::text, null::text, v_existing.id, v_existing.flow_generation, true;
        end if;
        return;
    end if;

    -- (2) El control del flujo.
    select control.* into strict v_control
      from public.flow_controls control where control.flow_id = v_run.flow_id
     for share;
    if public._flow_engine_run_turned_off(v_case.id) then
        return query select 'terminado'::text, 'flujo_apagado'::text, null::uuid, v_control.generation, false;
        return;
    end if;
    if v_control.state = 'pausado' then
        return query select 'rechazado'::text, 'flujo_pausado'::text, null::uuid, v_control.generation, false;
        return;
    end if;
    if p_require_legacy_dispatch and not v_run.legacy_dispatch_ok then
        return query select 'rechazado'::text, 'flow_engine_needs_engine_image'::text, null::uuid,
            v_control.generation, false;
        return;
    end if;
    v_clock := clock_timestamp();
    if p_now is null or p_now < v_clock - interval '5 minutes' or p_now > v_clock + interval '5 minutes' then
        return query select 'rechazado'::text, 'pilot_request_time_invalid'::text, null::uuid,
            v_control.generation, false;
        return;
    end if;

    -- (3) La bolsa.
    select runtime.* into v_pool
      from public.pilot_runtime_controls runtime where runtime.scope_key = v_run.pool
     for update;
    if not found then
        return query select 'rechazado'::text, 'flow_engine_pool_unknown'::text, null::uuid,
            v_control.generation, false;
        return;
    end if;
    select engine_row.* into v_existing
      from public.flow_engine_request_authorizations engine_row where engine_row.attempt_id = p_attempt_id;
    if found then
        if v_existing.action_id <> p_action_id then
            return query select 'rechazado'::text, 'pilot_attempt_mismatch'::text, null::uuid, null::bigint, false;
        else
            return query select 'authorized'::text, null::text, v_existing.id, v_existing.flow_generation, true;
        end if;
        return;
    end if;

    -- Lo estatico: el intento reservado, el modo del canal, la identidad y la
    -- cuenta y el inbox del scope del binding.
    select attempt.* into v_attempt
      from public.followup_delivery_attempts attempt
     where attempt.id = p_attempt_id and attempt.action_id = p_action_id;
    select binding.* into v_binding
      from public.pilot_recovery_case_bindings binding where binding.recovery_case_id = v_case.id;
    select scope.* into v_scope
      from public.pilot_scope_versions scope
     where scope.scope_key = v_binding.scope_key and scope.version = v_binding.scope_version;
    if v_attempt.id is null or v_attempt.phase <> 'reserved' or v_attempt.outcome is not null
       or v_binding.recovery_case_id is null or v_scope.scope_key is null then
        return query select 'rechazado'::text, 'pilot_attempt_mismatch'::text, null::uuid,
            v_control.generation, false;
        return;
    end if;
    if v_attempt.channel <> 'whatsapp'
       or (v_scope.channel_provider = 'waba' and v_attempt.mode <> 'approved_template')
       or (v_scope.channel_provider <> 'waba' and v_attempt.mode <> 'freeform') then
        return query select 'rechazado'::text, 'pilot_delivery_mode_mismatch'::text, null::uuid,
            v_control.generation, false;
        return;
    end if;
    select identity_row.* into v_identity
      from public.channel_identities identity_row
     where identity_row.id = v_case.selected_channel_identity_id
       and identity_row.contact_id = v_case.contact_id
       and identity_row.channel = 'whatsapp'
       and identity_row.identity_status = 'active';
    if not found
       or v_identity.account_id <> 'chatwoot:' || v_scope.chatwoot_account_id::text
       or coalesce(v_identity.metadata ->> 'inbox_id', '') <> v_scope.chatwoot_inbox_id::text then
        return query select 'rechazado'::text, 'pilot_attempt_mismatch'::text, null::uuid,
            v_control.generation, false;
        return;
    end if;

    -- La audiencia otra vez (principio 6), con la intencion del caso FOR SHARE.
    perform 1 from public.purchase_intents intent
     where intent.id = v_binding.audience_purchase_intent_id
     for share;
    select audience.* into v_audience
      from public._lancemos_pilot_audience_intent(
          v_binding.scope_key, v_binding.scope_version, v_case.contact_id, v_case.offer_code,
          v_binding.audience_purchase_intent_id, v_identity.external_user_id) audience;
    if v_audience.reason_code is distinct from 'pilot_audience_allowed' then
        return query select 'rechazado'::text,
            case when v_audience.reason_code like '%prior_opt_out' then 'baja'
                 when v_audience.reason_code like '%not_live' then 'ya_no_entra'
                 else 'no_entra:permiso' end,
            null::uuid, v_control.generation, false;
        return;
    end if;

    -- Los topes de la definicion fijada, sobre la bolsa, por los dos caminos:
    -- el total primero, despues el dia local en la zona de la politica.
    select definition_row.definition into strict v_definition
      from public.flow_definitions definition_row
     where definition_row.flow_id = v_run.flow_id and definition_row.version = v_run.version;
    select policy.* into strict v_policy
      from public.followup_policy_versions policy
     where policy.policy_key = v_action.policy_key and policy.version = v_action.policy_version;
    v_local_date := (v_clock at time zone v_policy.timezone)::date;
    select (select count(*)::integer from public.pilot_outbound_request_authorizations authrow
             where authrow.scope_key = v_run.pool)
           + (select count(*)::integer from public.flow_engine_request_authorizations engine_row
               where engine_row.pool = v_run.pool)
      into v_total;
    if jsonb_typeof(v_definition #> '{topes,total}') = 'number'
       and v_total >= (v_definition #>> '{topes,total}')::numeric then
        return query select 'rechazado'::text, 'tope_del_flujo'::text, null::uuid, v_control.generation, false;
        return;
    end if;
    select (select count(*)::integer from public.pilot_outbound_request_authorizations authrow
             where authrow.scope_key = v_run.pool and authrow.local_budget_date = v_local_date)
           + (select count(*)::integer from public.flow_engine_request_authorizations engine_row
               where engine_row.pool = v_run.pool and engine_row.local_budget_date = v_local_date)
      into v_daily;
    if jsonb_typeof(v_definition #> '{topes,por_dia}') = 'number'
       and v_daily >= (v_definition #>> '{topes,por_dia}')::numeric then
        return query select 'rechazado'::text, 'tope_del_flujo'::text, null::uuid, v_control.generation, false;
        return;
    end if;

    insert into public.flow_engine_request_authorizations (
        flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id, local_budget_date,
        flow_generation, authorized_at)
    values (
        v_run.flow_id, v_run.version, v_run.pool, v_run.legacy_scope_key, p_action_id, p_attempt_id,
        v_case.contact_id, v_local_date, v_control.generation, v_clock)
    returning id into v_id;
    return query select 'authorized'::text, null::text, v_id, v_control.generation, false;
end;
$function$;

-- El arranque de un envio del motor (contrato, «El arranque del motor»). La
-- llama la T2.4. outcome:
--   request_started  el intento arranco (o ya habia arrancado: replayed);
--   rechazado        no arranca ahora: deja su fila en
--                    flow_engine_start_rejections (phase despacho) y el intento
--                    queda reservado (el bridge reintenta en el lease siguiente);
--   terminado        el flujo se apago despues de arrancar: cierra el intento
--                    (failed_before_request), la accion, la secuencia y el caso
--                    con flujo_apagado.
-- Sin recorrido del motor, 55000 flow_engine_run_not_found.
create function public.mark_flow_engine_request_started_v1(
    p_action_id uuid, p_attempt_id uuid, p_worker_id text, p_lease_generation bigint, p_now timestamptz)
returns table (outcome text, reason text, id uuid, action_id uuid, idempotency_key text, attempt_number integer,
               channel text, mode text, phase text, lease_generation bigint, request_started_at timestamptz,
               expected_case_version bigint, expected_sequence_revision bigint, flow_authorization_id uuid,
               flow_generation bigint, authorization_replayed boolean)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_run public.flow_engine_runs%rowtype;
    v_auth record;
    v_outcome text;
    v_reason text;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_identity public.channel_identities%rowtype;
    v_action public.scheduled_actions%rowtype;
    v_sequence public.followup_sequences%rowtype;
    v_message text;
    v_auth_id uuid;
    v_generation bigint;
    v_replayed boolean := false;
begin
    if p_action_id is null or p_attempt_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_request_start_parameters';
    end if;
    select run.* into v_run
      from public.scheduled_actions action_row
      join public.flow_engine_runs run on run.recovery_case_id = action_row.recovery_case_id
     where action_row.id = p_action_id;
    if not found then
        raise exception using errcode = '55000', message = 'flow_engine_run_not_found';
    end if;

    -- La autorizacion y el arranque van juntos: un rechazo despues de
    -- autorizar (la baja) deshace la autorizacion y no consume cupo.
    begin
        select auth.* into v_auth
          from public._flow_engine_authorize_start(p_action_id, p_attempt_id, p_now, false) auth;
        v_outcome := v_auth.outcome;
        v_reason := v_auth.reason;
        v_auth_id := v_auth.authorization_id;
        v_generation := v_auth.flow_generation;
        v_replayed := coalesce(v_auth.replayed, false);
        if v_auth.outcome = 'authorized' then
            select case_row.* into strict v_case from public.recovery_cases case_row where case_row.id = v_run.recovery_case_id;
            select identity_row.* into v_identity
              from public.channel_identities identity_row where identity_row.id = v_case.selected_channel_identity_id;
            if not v_replayed
               and public._portable_chatwoot_opt_out_stop(
                   substring(v_identity.account_id from '^chatwoot:([0-9]+)$')::bigint,
                   v_case.contact_id, v_identity.external_user_id) then
                raise exception using errcode = '55000', message = 'flow_engine_start_rejected', detail = 'baja';
            end if;
            select attempt.* into strict v_attempt
              from public.mark_followup_request_started(
                  p_action_id, p_attempt_id, p_worker_id, p_lease_generation, p_now) attempt;
            v_outcome := 'request_started';
        end if;
    exception when sqlstate '55000' then
        get stacked diagnostics v_message = message_text;
        if v_message in ('flow_engine_start_rejected', 'pending_chatwoot_opt_out_stop') then
            v_outcome := 'rechazado';
            v_reason := 'baja';
            v_auth_id := null;
            v_replayed := false;
        else
            raise;
        end if;
    end;

    if v_outcome = 'rechazado' then
        perform public._flow_engine_record_start_rejection(
            v_run.flow_id, v_run.version, p_action_id, 'despacho', v_reason);
        select attempt.* into v_attempt
          from public.followup_delivery_attempts attempt
         where attempt.id = p_attempt_id and attempt.action_id = p_action_id;
        v_auth_id := null;
    elsif v_outcome = 'terminado' then
        -- El orden global: contacto -> caso -> secuencia -> accion -> intento.
        perform 1 from public.contacts contact_row
          join public.recovery_cases case_row on case_row.contact_id = contact_row.id
         where case_row.id = v_run.recovery_case_id
           for update of contact_row;
        select case_row.* into strict v_case
          from public.recovery_cases case_row where case_row.id = v_run.recovery_case_id for update;
        select action_row.* into strict v_action
          from public.scheduled_actions action_row where action_row.id = p_action_id;
        select sequence_row.* into strict v_sequence
          from public.followup_sequences sequence_row where sequence_row.id = v_action.followup_sequence_id
         for update;
        select action_row.* into strict v_action
          from public.scheduled_actions action_row where action_row.id = p_action_id for update;
        select attempt.* into v_attempt
          from public.followup_delivery_attempts attempt
         where attempt.id = p_attempt_id and attempt.action_id = p_action_id
         for update;
        if not (v_action.lease_owner = p_worker_id
                and v_action.lease_generation = p_lease_generation
                and v_action.lease_expires_at > p_now
                and v_attempt.id is not null
                and v_attempt.lease_generation = p_lease_generation
                and v_attempt.phase = 'reserved') then
            raise exception using errcode = 'P0002', message = 'current_action_lease_not_found';
        end if;
        update public.followup_delivery_attempts attempt
           set phase = 'completed', outcome = 'failed_before_request', reason_code = 'flujo_apagado',
               updated_at = clock_timestamp()
         where attempt.id = p_attempt_id
        returning attempt.* into v_attempt;
        update public.scheduled_actions action_row
           set status = 'cancelled', terminal_reason = 'flujo_apagado', next_attempt_at = null,
               lease_owner = null, lease_expires_at = null
         where action_row.id = p_action_id;
        update public.followup_sequences sequence_row
           set status = 'completed', completion_reason = 'flujo_apagado', completed_at = p_now,
               revision = sequence_row.revision + 1
         where sequence_row.id = v_sequence.id and sequence_row.status in ('active', 'paused')
        returning sequence_row.* into v_sequence;
        update public.recovery_cases case_row
           set status = 'cancelled', closed_at = p_now, version = case_row.version + 1
         where case_row.id = v_case.id and case_row.status in ('grace_period', 'active', 'paused')
        returning case_row.* into v_case;
        select case_row.* into strict v_case from public.recovery_cases case_row where case_row.id = v_run.recovery_case_id;
        select sequence_row.* into strict v_sequence
          from public.followup_sequences sequence_row where sequence_row.id = v_action.followup_sequence_id;
        insert into public.conversation_events (
            recovery_case_id, event_type, actor_type, related_action_id, data)
        values (
            v_case.id, 'followup_action_reevaluated', 'system', p_action_id,
            jsonb_build_object('decision', 'cancel', 'reason_code', 'flujo_apagado',
                               'worker_id', p_worker_id,
                               'policy_key', v_action.policy_key,
                               'policy_version', v_action.policy_version,
                               'case_version', v_case.version,
                               'sequence_revision', v_sequence.revision,
                               'lease_generation', p_lease_generation));
        v_auth_id := null;
    end if;

    return query select v_outcome, v_reason, v_attempt.id, v_attempt.action_id, v_attempt.idempotency_key,
        v_attempt.attempt_number, v_attempt.channel, v_attempt.mode, v_attempt.phase, v_attempt.lease_generation,
        v_attempt.request_started_at, v_attempt.expected_case_version, v_attempt.expected_sequence_revision,
        v_auth_id, v_generation, case when v_outcome = 'request_started' then v_replayed else false end;
end;
$function$;

-- La delegacion en el arranque de hoy y la aceptacion de la autorizacion del
-- motor, sobre su definicion vigente.
do $patch_start$
declare
    v_signatures text[] := array[
        'public.mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamptz)',
        'public.mark_followup_request_started(uuid,uuid,text,bigint,timestamptz)'
    ];
    v_anchors text[] := array[
        E'    -- flow_engine_legacy_guard: begin\n',
        E'    if not exists (\n'
        || E'        select 1\n'
        || E'        from public.pilot_outbound_request_authorizations authrow\n'
        || E'        where authrow.action_id = p_action_id\n'
        || E'          and authrow.attempt_id = p_attempt_id\n'
        || E'    ) then\n'
    ];
    v_inserts text[] := array[
        E'    -- flow_engine_motor_delegation: begin\n'
        || E'    -- La T2.3b (20261022000100): un caso del motor (con su renglon en\n'
        || E'    -- flow_engine_runs) arranca por la autorizacion del motor; la imagen\n'
        || E'    -- de hoy arranca los tres toques por aca. Los motivos, sin dos puntos.\n'
        || E'    declare\n'
        || E'        v_flow_engine_auth record;\n'
        || E'        v_flow_engine_case public.recovery_cases%rowtype;\n'
        || E'        v_flow_engine_identity public.channel_identities%rowtype;\n'
        || E'        v_flow_engine_attempt public.followup_delivery_attempts%rowtype;\n'
        || E'    begin\n'
        || E'        if exists (\n'
        || E'            select 1 from public.scheduled_actions engine_action\n'
        || E'              join public.flow_engine_runs engine_run\n'
        || E'                on engine_run.recovery_case_id = engine_action.recovery_case_id\n'
        || E'             where engine_action.id = p_action_id\n'
        || E'        ) then\n'
        || E'            select engine_auth.outcome as engine_outcome, engine_auth.reason as engine_reason,\n'
        || E'                   engine_auth.authorization_id as engine_authorization_id,\n'
        || E'                   engine_auth.flow_generation as engine_generation,\n'
        || E'                   engine_auth.replayed as engine_replayed\n'
        || E'              into v_flow_engine_auth\n'
        || E'              from public._flow_engine_authorize_start(p_action_id, p_attempt_id, p_now, true) engine_auth;\n'
        || E'            if v_flow_engine_auth.engine_outcome <> ''authorized'' then\n'
        || E'                raise exception using\n'
        || E'                    errcode = ''55000'',\n'
        || E'                    message = ''pilot_request_start_rejected'',\n'
        || E'                    detail = translate(v_flow_engine_auth.engine_reason, '':'', ''_'');\n'
        || E'            end if;\n'
        || E'            select engine_case.* into strict v_flow_engine_case\n'
        || E'              from public.scheduled_actions engine_action\n'
        || E'              join public.recovery_cases engine_case on engine_case.id = engine_action.recovery_case_id\n'
        || E'             where engine_action.id = p_action_id;\n'
        || E'            select engine_identity.* into v_flow_engine_identity\n'
        || E'              from public.channel_identities engine_identity\n'
        || E'             where engine_identity.id = v_flow_engine_case.selected_channel_identity_id;\n'
        || E'            if not v_flow_engine_auth.engine_replayed\n'
        || E'               and public._portable_chatwoot_opt_out_stop(\n'
        || E'                   substring(v_flow_engine_identity.account_id from ''^chatwoot:([0-9]+)$'')::bigint,\n'
        || E'                   v_flow_engine_case.contact_id,\n'
        || E'                   v_flow_engine_identity.external_user_id) then\n'
        || E'                raise exception using\n'
        || E'                    errcode = ''55000'',\n'
        || E'                    message = ''pilot_request_start_rejected'',\n'
        || E'                    detail = ''pilot_chatwoot_opt_out_stop'';\n'
        || E'            end if;\n'
        || E'            select engine_attempt.* into strict v_flow_engine_attempt\n'
        || E'              from public.mark_followup_request_started(\n'
        || E'                  p_action_id, p_attempt_id, p_worker_id, p_lease_generation, p_now) engine_attempt;\n'
        || E'            return query select\n'
        || E'                v_flow_engine_attempt.id,\n'
        || E'                v_flow_engine_attempt.action_id,\n'
        || E'                v_flow_engine_attempt.idempotency_key,\n'
        || E'                v_flow_engine_attempt.attempt_number,\n'
        || E'                v_flow_engine_attempt.channel,\n'
        || E'                v_flow_engine_attempt.mode,\n'
        || E'                v_flow_engine_attempt.phase,\n'
        || E'                v_flow_engine_attempt.lease_generation,\n'
        || E'                v_flow_engine_attempt.expected_case_version,\n'
        || E'                v_flow_engine_attempt.expected_sequence_revision,\n'
        || E'                v_flow_engine_auth.engine_authorization_id,\n'
        || E'                v_flow_engine_auth.engine_generation,\n'
        || E'                v_flow_engine_auth.engine_replayed;\n'
        || E'            return;\n'
        || E'        end if;\n'
        || E'    end;\n'
        || E'    -- flow_engine_motor_delegation: end\n'
        || E'\n',
        E'    -- flow_engine_motor_authorization: begin\n'
        || E'    -- La T2.3b (20261022000100): un arranque autorizado por el motor\n'
        || E'    -- (flow_engine_request_authorizations) tambien cruza la frontera.\n'
        || E'    if exists (\n'
        || E'        select 1\n'
        || E'        from public.flow_engine_request_authorizations engine_row\n'
        || E'        where engine_row.action_id = p_action_id\n'
        || E'          and engine_row.attempt_id = p_attempt_id\n'
        || E'    ) then\n'
        || E'        return query\n'
        || E'        select *\n'
        || E'        from public._mark_followup_request_started_without_pilot_guard(\n'
        || E'            p_action_id,\n'
        || E'            p_attempt_id,\n'
        || E'            p_worker_id,\n'
        || E'            p_lease_generation,\n'
        || E'            p_now\n'
        || E'        );\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_motor_authorization: end\n'
        || E'\n'
    ];
    v_markers text[] := array['flow_engine_motor_delegation: begin', 'flow_engine_motor_authorization: begin'];
    v_definition text;
    i integer;
begin
    for i in 1 .. array_length(v_signatures, 1) loop
        if to_regprocedure(v_signatures[i]) is null then
            raise exception using errcode = '55000', message = 'flow_engine_motor_function_missing',
                detail = v_signatures[i];
        end if;
        if (select p.proowner from pg_proc p where p.oid = v_signatures[i]::regprocedure)
           is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
            raise exception using errcode = '55000', message = 'flow_engine_motor_owner_mismatch',
                detail = v_signatures[i];
        end if;
        v_definition := pg_get_functiondef(v_signatures[i]::regprocedure);
        if length(v_definition) - length(replace(v_definition, v_anchors[i], '')) <> length(v_anchors[i])
           or position(v_markers[i] in v_definition) > 0 then
            raise exception using errcode = '55000', message = 'unexpected_flow_engine_motor_definition',
                detail = v_signatures[i];
        end if;
        execute replace(v_definition, v_anchors[i], v_inserts[i] || v_anchors[i]);
        if position(v_markers[i] in pg_get_functiondef(v_signatures[i]::regprocedure)) = 0 then
            raise exception using errcode = '55000', message = 'flow_engine_motor_definition_not_replaced',
                detail = v_signatures[i];
        end if;
    end loop;
end;
$patch_start$;

do $acl_start$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._flow_engine_authorize_start(uuid,uuid,timestamptz,boolean)',
        'public.mark_flow_engine_request_started_v1(uuid,uuid,text,bigint,timestamptz)'
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
        grant execute on function public.mark_flow_engine_request_started_v1(uuid, uuid, text, bigint, timestamptz)
            to service_role;
    end if;
end;
$acl_start$;

-- ===========================================================================
-- Bloque 4. El contexto del paso y el apagado en la reevaluacion.
-- ===========================================================================

-- Que manda un paso del motor: la plantilla, el idioma, las variables y el
-- boton del envio de ese paso en la version fijada del recorrido (principio
-- 10). send_ordinal es la posicion del step_key en la forma de pasos del
-- legacy_kind. Cero filas para una accion del legado.
create function public.flow_engine_execution_context_v1(p_action_id uuid)
returns table (flow_id uuid, version integer, product_ref text, flow_key text, step_key text,
               send_ordinal integer, template_name text, template_language text, template_variables text[],
               template_button jsonb, legacy_dispatch_ok boolean)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select run.flow_id, run.version, run.product_ref, run.flow_key, action_row.step_key,
           position_row.ordinal, send.plantilla, send.idioma,
           array(select jsonb_array_elements_text(coalesce(send.variables, '[]'::jsonb))),
           send.boton, run.legacy_dispatch_ok
      from public.scheduled_actions action_row
      join public.flow_engine_runs run on run.recovery_case_id = action_row.recovery_case_id
      join public.flow_definitions definition_row
        on definition_row.flow_id = run.flow_id and definition_row.version = run.version
      cross join lateral (
          select shape.step_index::integer as ordinal
            from jsonb_array_elements(public._flow_engine_legacy_steps(run.legacy_kind))
                 with ordinality as shape(step, step_index)
           where shape.step ->> 'step_key' = action_row.step_key
      ) position_row
      join lateral public._flow_engine_sends(definition_row.definition) send
        on send.ordinal = position_row.ordinal
     where action_row.id = p_action_id
$function$;

-- El apagado en la reevaluacion, sobre su definicion vigente: (C) antes de la
-- decision, un execute o un defer de un caso del motor cuyo flujo se apago
-- despues de arrancar pasa a cancel/flujo_apagado (cualquier otro motivo le
-- gana: el orden de la puerta); (D) flujo_apagado cierra la secuencia y
-- cancela el caso, como la baja.
do $patch_reevaluate$
declare
    c_signature constant text :=
        'public.reevaluate_followup_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)';
    c_turned_off_anchor constant text := E'    if v_decision = ''defer'' then\n';
    c_close_anchor constant text := E'            ''purchase_detected_any_offer''\n        ) then\n';
    c_turned_off constant text :=
        E'    -- flow_engine_turned_off: begin\n'
        || E'    -- La T2.3b (20261022000100): un caso del motor cuyo flujo se apago\n'
        || E'    -- despues de arrancar no sale; cualquier otro motivo le gana.\n'
        || E'    if v_decision in (''execute'', ''defer'')\n'
        || E'       and public._flow_engine_run_turned_off(v_case.id) then\n'
        || E'        v_decision := ''cancel'';\n'
        || E'        v_reason := ''flujo_apagado'';\n'
        || E'    end if;\n'
        || E'    -- flow_engine_turned_off: end\n'
        || E'\n';
    c_close constant text :=
        E',\n'
        || E'            -- flow_engine_turned_off_close: begin\n'
        || E'            ''flujo_apagado''\n'
        || E'            -- flow_engine_turned_off_close: end';
    v_definition text;
begin
    if to_regprocedure(c_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_motor_function_missing', detail = c_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_motor_owner_mismatch', detail = c_signature;
    end if;
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, c_turned_off_anchor, '')) <> length(c_turned_off_anchor)
       or length(v_definition) - length(replace(v_definition, c_close_anchor, '')) <> length(c_close_anchor)
       or position('flow_engine_turned_off: begin' in v_definition) > 0
       or position('flow_engine_turned_off_close: begin' in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_motor_definition',
            detail = c_signature;
    end if;
    v_definition := replace(v_definition, c_turned_off_anchor, c_turned_off || c_turned_off_anchor);
    v_definition := replace(v_definition, c_close_anchor,
        E'            ''purchase_detected_any_offer''' || c_close || E'\n        ) then\n');
    execute v_definition;
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if position('flow_engine_turned_off: begin' in v_definition) = 0
       or position('flow_engine_turned_off_close: begin' in v_definition) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_motor_definition_not_replaced',
            detail = c_signature;
    end if;
end;
$patch_reevaluate$;

do $acl_context$
declare
    v_role text;
begin
    revoke all on function public.flow_engine_execution_context_v1(uuid) from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function public.flow_engine_execution_context_v1(uuid) from %I', v_role);
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.flow_engine_execution_context_v1(uuid) to service_role;
    end if;
end;
$acl_context$;

commit;
