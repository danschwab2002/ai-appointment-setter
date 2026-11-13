-- e483-native-person-cap-v1: el tope de mensajes proactivos por persona en el
-- motor nativo (E-483, C6 del mapa «el AE maneja todos los flujos», 2026-11-12;
-- D5: 4 mensajes cada 5 dias, sumando flujos nativos y disparos por audiencia,
-- apagado por defecto). Contrato: docs/contracts/flow-engine-v2.md («El tope por
-- persona en el nativo»).
--
-- Por que. Con varios toques (C1) y las difusiones de E-484, nada limita cuantos
-- mensajes proactivos recibe una misma persona sumando los flujos nativos: un
-- numero nuevo se quema. El tope de 20261007000100 (pilot_proactive_contact_caps)
-- es del legado: no convive con una ruta en motor (el disparador
-- pilot_proactive_contact_caps_no_engine_route) y, prendido, rompe la adopcion de
-- la respuesta: el arranque frenado queda reservado, la accion sigue viva hasta
-- vencer, y la adopcion (20261001000400, condicion 3) no adopta mientras la
-- persona tenga una accion viva. Este tope es otro: el del nativo, y no se toca
-- el del legado.
--
-- Que trae:
-- 1. mark_flow_engine_request_started_v2 (nueva): el mismo arranque que la v1 con
--    un parametro mas, p_person_cap {"max_sends": N, "window_hours": H} (N de 1 a
--    20, H de 1 a 720). Lo deja en la transaccion (set_config local,
--    flow_engine.native_person_cap) y delega en la v1. El bridge la llama solo con
--    FLOW_ENGINE_NATIVE_PERSON_CAP_ENABLED; sin el flag llama a la v1 como hoy y
--    el tope no existe. Los numeros son de la instancia (Settings del bridge).
-- 2. _flow_engine_authorize_native_tail: con el tope en la transaccion, justo
--    antes de insertar la autorizacion (despues de los topes de la bolsa, que
--    siguen igual: un toque frenado por la bolsa se reintenta), toma un lock por
--    persona (tenant + telefono canonico, el patron de 20261007000100) y cuenta
--    los envios nativos autorizados de la persona en la ventana: las filas de
--    flow_engine_request_authorizations de una accion de un recorrido nativo
--    (flow_engine_native_runs) del mismo tenant, con autorizacion dentro de las
--    ultimas H horas. La persona es el contacto y cualquier contacto de la misma
--    cuenta de Chatwoot con una identidad de WhatsApp en alguna de las formas del
--    telefono (_whatsapp_phone_variants). Los disparos por audiencia (E-484) son
--    recorridos nativos: cuentan igual. Llegado el tope devuelve terminado con
--    tope_por_persona: no inserta y no consume bolsa.
-- 3. mark_flow_engine_request_started_v1, el cierre de terminado: el motivo deja
--    de ser el literal flujo_apagado y pasa a ser el de la autorizacion (que para
--    el flujo apagado es flujo_apagado: el cierre de hoy no cambia). Asi el toque
--    frenado por el tope cierra el intento, la accion, la secuencia y el caso con
--    tope_por_persona.
--
-- Decisiones (con su default, D-C6-1 a D-C6-4):
-- D-C6-1. El toque que pasaria el tope cierra con tope_por_persona, no se
--    pospone. Posponerlo es dejar la accion viva hasta que la ventana se libere,
--    y una accion viva frena la adopcion de la respuesta de esa persona (el
--    choque del tope del legado). Cerrado, no queda ninguna accion viva.
-- D-C6-2. Lo que no cuenta: la respuesta a quien escribio. El seguimiento de una
--    conversacion (claim_flow_engine_silence_v1, 20261101000200: su
--    autorizacion no tiene accion programada) no cuenta ni se frena; tampoco el
--    agente contestando. Cuenta lo que el motor nativo manda primero.
-- D-C6-3. El legado no cuenta ni se frena: ni el carrito de ATT1 en ruta motor
--    (sin recorrido nativo) ni pilot_outbound_request_authorizations. El tope del
--    legado sigue siendo el de 20261007000100.
-- D-C6-4. Cuenta autorizaciones, como el tope del legado: un envio que Meta
--    rechaza despues tambien cuenta.
--
-- Como: el metodo de 20261112210000, sobre la definicion VIGENTE de cada
-- funcion, con un texto exacto que tiene que aparecer una sola vez, la marca que
-- no puede estar, el duenio que aplica y los permisos que no cambian. Correrla
-- dos veces aborta con 55000. No crea tablas ni toca filas.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)') is null
       or to_regprocedure('public.mark_flow_engine_request_started_v1(uuid,uuid,text,bigint,timestamptz)') is null
       or to_regprocedure('public._whatsapp_phone_variants(text)') is null
       or to_regprocedure('public._whatsapp_phone_canonical(text)') is null then
        raise exception using errcode = '55000', message = 'e483_native_person_cap_missing';
    end if;
    if to_regprocedure('public.mark_flow_engine_request_started_v2(uuid,uuid,text,bigint,timestamptz,jsonb)') is not null
       or position('e483_native_person_cap' in pg_get_functiondef(
              'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)'::regprocedure)) > 0 then
        raise exception using errcode = '55000', message = 'e483_native_person_cap_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla: cada ancla una sola vez, la marca ninguna; despues, la marca.
create function pg_temp.e483_native_person_cap_patch(
    p_signature text, p_anchors text[], p_replacements text[], p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
    v_acl text;
    v_index integer;
begin
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'e483_native_person_cap_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_e483_native_person_cap_definition',
            detail = p_signature || ' ' || p_marker;
    end if;
    for v_index in 1 .. array_length(p_anchors, 1) loop
        if length(v_definition) - length(replace(v_definition, p_anchors[v_index], ''))
               <> length(p_anchors[v_index]) then
            raise exception using errcode = '55000', message = 'unexpected_e483_native_person_cap_definition',
                detail = p_signature || ' ' || v_index::text;
        end if;
        v_definition := replace(v_definition, p_anchors[v_index], p_replacements[v_index]);
    end loop;
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = p_signature::regprocedure;
    execute v_definition;
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_native_person_cap_not_replaced',
            detail = p_signature;
    end if;
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from v_acl then
        raise exception using errcode = '55000', message = 'e483_native_person_cap_acl_changed',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- 2. El tope en la autorizacion nativa, antes de insertarla.
-- ===========================================================================
do $tail$
declare
    c_anchor constant text :=
        E'\n    insert into public.flow_engine_request_authorizations (\n'
        || E'        flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id, local_budget_date,\n';
    c_block constant text :=
        E'\n    -- e483_native_person_cap: begin (20261112230000)\n'
        || E'    -- El tope por persona del nativo, solo con el de la instancia en la\n'
        || E'    -- transaccion (mark_flow_engine_request_started_v2). Lock por persona y\n'
        || E'    -- conteo de sus envios nativos en la ventana; llegado el tope, cierra.\n'
        || E'    declare\n'
        || E'        v_person_cap jsonb;\n'
        || E'        v_person_cap_phone text;\n'
        || E'        v_person_cap_recent integer;\n'
        || E'    begin\n'
        || E'        v_person_cap := nullif(current_setting(''flow_engine.native_person_cap'', true), '''')::jsonb;\n'
        || E'        if v_person_cap is not null then\n'
        || E'            if jsonb_typeof(v_person_cap -> ''max_sends'') is distinct from ''number''\n'
        || E'               or jsonb_typeof(v_person_cap -> ''window_hours'') is distinct from ''number'' then\n'
        || E'                raise exception using errcode = ''22023'', message = ''invalid_native_person_cap'';\n'
        || E'            end if;\n'
        || E'            v_person_cap_phone := coalesce(\n'
        || E'                public._whatsapp_phone_canonical(v_identity.external_user_id),\n'
        || E'                v_identity.external_user_id\n'
        || E'            );\n'
        || E'            perform pg_advisory_xact_lock(hashtextextended(\n'
        || E'                ''flow-engine-native-person-cap:'' || p_control.tenant_ref\n'
        || E'                    || '':'' || coalesce(v_person_cap_phone, p_case.contact_id::text),\n'
        || E'                0\n'
        || E'            ));\n'
        || E'            select count(*)::integer into v_person_cap_recent\n'
        || E'              from public.flow_engine_request_authorizations cap_row\n'
        || E'              join public.flow_controls cap_control on cap_control.flow_id = cap_row.flow_id\n'
        || E'             where cap_control.tenant_ref = p_control.tenant_ref\n'
        || E'               and cap_row.authorized_at > p_clock\n'
        || E'                   - make_interval(hours => (v_person_cap ->> ''window_hours'')::integer)\n'
        || E'               and exists (\n'
        || E'                   select 1 from public.scheduled_actions cap_action\n'
        || E'                     join public.flow_engine_native_runs cap_native\n'
        || E'                       on cap_native.recovery_case_id = cap_action.recovery_case_id\n'
        || E'                    where cap_action.id = cap_row.action_id)\n'
        || E'               and (\n'
        || E'                   cap_row.contact_id = p_case.contact_id\n'
        || E'                   or cap_row.contact_id in (\n'
        || E'                       select other.contact_id\n'
        || E'                         from public.channel_identities other\n'
        || E'                        where other.channel = ''whatsapp''\n'
        || E'                          and other.account_id = v_identity.account_id\n'
        || E'                          and other.external_user_id = any(\n'
        || E'                              coalesce(\n'
        || E'                                  public._whatsapp_phone_variants(v_identity.external_user_id),\n'
        || E'                                  array[v_identity.external_user_id]\n'
        || E'                              )\n'
        || E'                          )\n'
        || E'                   )\n'
        || E'               );\n'
        || E'            if v_person_cap_recent >= (v_person_cap ->> ''max_sends'')::integer then\n'
        || E'                return query select ''terminado''::text, ''tope_por_persona''::text, null::uuid,\n'
        || E'                    p_control.generation, false;\n'
        || E'                return;\n'
        || E'            end if;\n'
        || E'        end if;\n'
        || E'    end;\n'
        || E'    -- e483_native_person_cap: end\n';
begin
    perform pg_temp.e483_native_person_cap_patch(
        'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)',
        array[c_anchor], array[c_block || c_anchor], 'e483_native_person_cap: begin');
end;
$tail$;

-- ===========================================================================
-- 3. El cierre de terminado con el motivo de la autorizacion.
-- ===========================================================================
do $close$
begin
    perform pg_temp.e483_native_person_cap_patch(
        'public.mark_flow_engine_request_started_v1(uuid,uuid,text,bigint,timestamptz)',
        array[
            E'    elsif v_outcome = ''terminado'' then\n',
            E'outcome = ''failed_before_request'', reason_code = ''flujo_apagado'',\n',
            E'set status = ''cancelled'', terminal_reason = ''flujo_apagado'', next_attempt_at = null,\n',
            E'set status = ''completed'', completion_reason = ''flujo_apagado'', completed_at = p_now,\n',
            E'jsonb_build_object(''decision'', ''cancel'', ''reason_code'', ''flujo_apagado'',\n'
        ],
        array[
            E'    elsif v_outcome = ''terminado'' then\n'
            || E'        -- e483_native_person_cap: el motivo de la autorizacion (flujo_apagado o\n'
            || E'        -- tope_por_persona, 20261112230000).\n'
            || E'        v_reason := coalesce(v_reason, ''flujo_apagado'');\n',
            E'outcome = ''failed_before_request'', reason_code = v_reason,\n',
            E'set status = ''cancelled'', terminal_reason = v_reason, next_attempt_at = null,\n',
            E'set status = ''completed'', completion_reason = v_reason, completed_at = p_now,\n',
            E'jsonb_build_object(''decision'', ''cancel'', ''reason_code'', v_reason,\n'
        ],
        'e483_native_person_cap');
end;
$close$;

-- ===========================================================================
-- 1. El arranque con el tope de la instancia.
-- ===========================================================================
create function public.mark_flow_engine_request_started_v2(
    p_action_id uuid, p_attempt_id uuid, p_worker_id text, p_lease_generation bigint, p_now timestamptz,
    p_person_cap jsonb)
returns table (outcome text, reason text, id uuid, action_id uuid, idempotency_key text, attempt_number integer,
               channel text, mode text, phase text, lease_generation bigint, request_started_at timestamptz,
               expected_case_version bigint, expected_sequence_revision bigint, flow_authorization_id uuid,
               flow_generation bigint, authorization_replayed boolean)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
-- e483_native_person_cap (20261112230000): la v1 con el tope por persona de la
-- instancia en la transaccion.
begin
    if p_person_cap is null or jsonb_typeof(p_person_cap) <> 'object'
       or (select count(*) from jsonb_object_keys(p_person_cap)) <> 2
       or jsonb_typeof(p_person_cap -> 'max_sends') is distinct from 'number'
       or jsonb_typeof(p_person_cap -> 'window_hours') is distinct from 'number'
       or (p_person_cap ->> 'max_sends') !~ '^[0-9]{1,2}$'
       or (p_person_cap ->> 'window_hours') !~ '^[0-9]{1,3}$'
       or (p_person_cap ->> 'max_sends')::integer not between 1 and 20
       or (p_person_cap ->> 'window_hours')::integer not between 1 and 720 then
        raise exception using errcode = '22023', message = 'invalid_native_person_cap';
    end if;
    perform set_config('flow_engine.native_person_cap',
        jsonb_build_object('max_sends', (p_person_cap ->> 'max_sends')::integer,
                           'window_hours', (p_person_cap ->> 'window_hours')::integer)::text,
        true);
    return query select started.*
      from public.mark_flow_engine_request_started_v1(
          p_action_id, p_attempt_id, p_worker_id, p_lease_generation, p_now) started;
end;
$function$;

do $acl$
declare
    v_role text;
begin
    revoke all on function public.mark_flow_engine_request_started_v2(uuid, uuid, text, bigint, timestamptz, jsonb)
        from public;
    for v_role in
        select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function public.mark_flow_engine_request_started_v2(uuid, uuid, text, bigint, timestamptz, jsonb) from %I', v_role);
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.mark_flow_engine_request_started_v2(uuid, uuid, text, bigint, timestamptz, jsonb)
            to service_role;
    end if;
end;
$acl$;

commit;
