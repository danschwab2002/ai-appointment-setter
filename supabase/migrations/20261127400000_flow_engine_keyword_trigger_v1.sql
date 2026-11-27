-- flow-engine-keyword-trigger-v1: el flujo que arranca con la palabra clave de
-- una regla (Z2 3.9, segunda parte; la primera es keyword-rules-v1,
-- 20261126400000). Contrato: docs/contracts/keyword-rules-v1.md §5.
--
-- Por que. La lista definitiva del 2026-11-20 pide que cada link wa.me con
-- texto por segmento (soporte, check-in, asistencia, alumnos) «lleve a su
-- flujo». La regla v1 pone la etiqueta y la nota del turno; el motor nativo
-- admitia solo eventos de Hotmart, del formulario, silencios y audiencias.
--
-- Que cambia:
--   1. admit_flow_engine_event_v1: solo con p_instance.native_keyword = true (el
--      bridge la manda con KEYWORD_RULES_ENABLED, FLOW_ENGINE_ENABLED y
--      KEYWORD_FLOW_TRIGGER_ENABLED) y un evento con fuente: palabra_clave, la
--      admision va a la funcion nueva del punto 2. Sin la clave, o con otra
--      fuente, el cuerpo de antes: un evento de palabra clave sin la clave es
--      invalid_flow_engine_event (22023), como cualquier forma desconocida.
--   2. _flow_engine_admit_native_keyword_v1 (nueva, privada): la forma exacta
--      del evento (tipo de fuente, evento_id = palabra_clave:<conversacion>:
--      <mensaje>, regla_id, regla_clave, momento, chatwoot con cuenta, bandeja,
--      conversacion y mensaje, destino). La regla tiene que estar publicada, en
--      la instancia y con esa clave (si no: sin_flujo, sin fila). Los flujos son
--      los de la instancia con ruta nativa, no apagados, cuya version activa
--      dispara con {palabra_clave: <esa clave>} y de un producto atado (si no
--      hay: sin_flujo). El contacto lo resuelve la base desde Chatwoot: el de la
--      identidad de WhatsApp activa del destino en esa cuenta (la que dejo la
--      admision de la conversacion entrante); el bridge no manda un contacto ni
--      un nombre. Sin identidad, cada flujo no arranca con
--      no_entra:sin_contacto. El evento persistido es uno sintetico de
--      webhook_events (source system, FLOW_KEYWORD_MATCHED, external_event_id
--      keyword:<regla>:<conversacion>:<mensaje>, sin telefono): hace la
--      admision idempotente por mensaje. Despues, cada flujo en su
--      subtransaccion por _flow_engine_admit_native_flow, como un evento de
--      Hotmart: la cohorte, la baja, la repeticion, no_arranca_mientras, la
--      bolsa, la politica y el recorrido.
--   3. _flow_engine_native_trigger: palabra_clave, si dispara es exactamente
--      {palabra_clave: <texto>}.
--   4. _flow_engine_native_steps: nativo:palabra_clave, hasta tres toques con
--      nombre y producto y sin cupon.
--   5. _flow_engine_native_unsupported: entra con permiso conversacion (la
--      persona nos escribio), sin contesto_a (no es una respuesta a una
--      plantilla), y puede tener varios toques con las reglas de siempre. Como
--      con la audiencia, la base lo abre y lo que no deja publicarlo con un
--      flag apagado es el validador del bridge (motor_nativo:disparador,
--      NativeRuntime.native_keyword).
--   6. _flow_engine_admit_native_flow: el permiso de la palabra clave
--      (_flow_engine_native_keyword_reason, nueva: la conversacion derivada y,
--      con la salida compra, una compra del producto), el caso landing (la
--      check de recovery_cases.source no admite system) con la regla y la
--      conversacion en el context, y la autorizacion con la evidencia
--      keyword_rule.
--   7. get_followup_execution_context: un caso nativo de palabra clave nace sin
--      oferta, como uno de audiencia: la columna flow_engine_audience_product_ref
--      trae el producto de su flujo, del context del caso (el despacho lo valida contra los
--      productos declarados, E-661), y buyer_phone, si el contacto no tiene
--      telefono, el numero de WhatsApp de la identidad del caso (la misma que
--      ya eligio la admision). No se escribe ningun dato en contacts.
--   8. _portable_case_purchase_stop_reason: un caso de palabra clave con la
--      salida compra se corta con una compra del producto de la persona
--      (purchase_detected_any_offer).
--   9. flow_engine_native_runs: trigger_event admite palabra_clave.
--
-- Lo que NO cambia. Ningun flujo publicado tiene dispara.palabra_clave (el
-- validador lo marcaba disparador desconocido), asi que la forma, la apertura
-- y la admision de todo lo que corre en ATT1 y Maribel son las de antes. Sin
-- la clave en p_instance la admision es la de antes byte a byte. La puerta
-- antes de cada envio (la baja del contacto y la de Chatwoot de ese movil, el
-- tope por persona del bridge, los frenos ante Meta del catalogo) es la misma
-- para cualquier recorrido nativo.
--
-- Decisiones (Z2 3.9, con su default; tambien en el reporte):
--   D-Z2.5 Una regla con varios flujos: arrancan en orden de flow_key y el
--          segundo de un mismo mensaje no arranca (evento_con_otro_flujo, la
--          regla de siempre de un evento por caso).
--   D-Z2.6 Sin identidad de WhatsApp del destino no se crea un contacto aca:
--          no_entra:sin_contacto. El contacto lo crea la admision de la
--          conversacion entrante, que corre antes del turno.
--   D-Z2.7 La admision no escribe nombre ni telefono en contacts: un paso con
--          la variable nombre y un contacto sin nombre cierra sin salir
--          (approved_template_parameters_missing del despacho, sin cambios).
--
-- Como: el metodo de 20261112210600, reemplazos contados sobre la definicion
-- VIGENTE de cada funcion, cada uno con su marca, que no puede estar. Correrla
-- dos veces aborta con 55000. El bloque 9 toma ACCESS EXCLUSIVE sobre
-- flow_engine_native_runs: va al final, con lock_timeout.
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
       or to_regprocedure('public._portable_case_purchase_stop_reason(uuid)') is null
       or to_regprocedure('public.get_followup_execution_context(uuid,text,bigint,timestamptz)') is null
       or to_regprocedure('public._flow_engine_native_audience_purchased(text,text,uuid,text,timestamptz)') is null
       or to_regprocedure('public._flow_engine_record_start_rejection(uuid,integer,uuid,text,text)') is null
       or to_regprocedure('public._flow_store_product_is_bound(text,text,text)') is null
       or to_regclass('public.keyword_rules') is null
       or position('e483_native_audience_admit: end' in pg_get_functiondef(
              'public.admit_flow_engine_event_v1(jsonb,jsonb)'::regprocedure)) = 0
       or position('flow_engine_native_audience_recipient' in pg_get_functiondef(
              'public.get_followup_execution_context(uuid,text,bigint,timestamptz)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_keyword_trigger_missing';
    end if;
    if to_regprocedure('public._flow_engine_admit_native_keyword_v1(jsonb,jsonb)') is not null
       or position('flow_engine_keyword_trigger' in pg_get_functiondef(
              'public.admit_flow_engine_event_v1(jsonb,jsonb)'::regprocedure)) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_keyword_trigger_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla: el ancla una sola vez, la marca ninguna; despues, la marca.
create function pg_temp.flow_engine_keyword_trigger_patch(
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
        raise exception using errcode = '55000', message = 'flow_engine_keyword_trigger_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_keyword_trigger_definition',
            detail = p_signature || ' ' || p_marker;
    end if;
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = p_signature::regprocedure;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_keyword_trigger_not_replaced',
            detail = p_signature || ' ' || p_marker;
    end if;
    -- create or replace conserva el duenio y los permisos: se verifica.
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from v_acl then
        raise exception using errcode = '55000', message = 'flow_engine_keyword_trigger_acl_changed',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- 3. El disparador.
-- ===========================================================================
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_native_trigger(jsonb)',
    E'        when p_definition -> ''dispara'' = ''{"evento": "compra"}''::jsonb then ''compra''\n',
    E'        -- flow_engine_keyword_trigger_kind: la palabra clave de una regla, exactamente\n'
    || E'        -- {palabra_clave: <texto>} (20261127400000).\n'
    || E'        when jsonb_typeof(p_definition -> ''dispara'') = ''object''\n'
    || E'             and (p_definition -> ''dispara'') - ''palabra_clave'' = ''{}''::jsonb\n'
    || E'             and jsonb_typeof(p_definition #> ''{dispara,palabra_clave}'') = ''string'' then ''palabra_clave''\n'
    || E'        when p_definition -> ''dispara'' = ''{"evento": "compra"}''::jsonb then ''compra''\n',
    'flow_engine_keyword_trigger_kind');

-- ===========================================================================
-- 4. La forma: hasta tres toques, con nombre y producto y sin cupon; con texto_fijo
--    como las demas formas nativas menos la ficha (20261127300000, F4).
-- ===========================================================================
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_native_steps(text)',
    -- Al final, despues del formulario: fuera del tramo de la audiencia que reemplaza 20261126700000
    -- (de e483_native_audience_steps a flow_engine_native_intent).
    E'    end\n$function$',
    E'        -- flow_engine_keyword_trigger_steps: la palabra clave, hasta tres toques sin cupon y con texto_fijo (20261127400000).\n'
    || E'        when ''nativo:palabra_clave'' then\n'
    || E'            ''[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
    || '"texto_fijo": true, "variables_de": ["nombre", "producto"], "coupon_variables": null}, '
    || '{"step_key": "native_touch_2", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
    || '"texto_fijo": true, "variables_de": ["nombre", "producto"], "coupon_variables": null}, '
    || '{"step_key": "native_touch_3", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
    || E'"texto_fijo": true, "variables_de": ["nombre", "producto"], "coupon_variables": null}]''::jsonb\n'
    || E'    end\n$function$',
    'flow_engine_keyword_trigger_steps');

-- ===========================================================================
-- 5. Lo que abre la base: el permiso, contesto_a y los toques.
-- ===========================================================================
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_native_unsupported(jsonb)',
    E'                  when ''silencio_agente'' then ''"conversacion"''\n',
    E'                  when ''silencio_agente'' then ''"conversacion"''\n'
    || E'                  -- flow_engine_keyword_trigger_permission: la persona nos escribio (20261127400000).\n'
    || E'                  when ''palabra_clave'' then ''"conversacion"''\n',
    'flow_engine_keyword_trigger_permission');
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_native_unsupported(jsonb)',
    E'        -- flow_engine_native_silence_entry: la conversacion adoptada, de cualquier plantilla.\n',
    E'        -- flow_engine_keyword_trigger_entry: la persona abrio la conversacion con el link, no\n'
    || E'        -- contesto una plantilla: sin contesto_a (20261127400000).\n'
    || E'        when public._flow_engine_native_trigger(p_definition) = ''palabra_clave''\n'
    || E'             and coalesce(p_definition #> ''{entra,contesto_a}'', ''[]''::jsonb) is distinct from ''[]''::jsonb\n'
    || E'            then ''motor_nativo:permiso''\n'
    || E'        -- flow_engine_native_silence_entry: la conversacion adoptada, de cualquier plantilla.\n',
    'flow_engine_keyword_trigger_entry');
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_native_unsupported(jsonb)',
    E'''pago_fallido'', ''audiencia'' /* e483_native_audience_multitouch */)\n',
    E'''pago_fallido'', ''audiencia'' /* e483_native_audience_multitouch */, ''palabra_clave'' /* flow_engine_keyword_trigger_multitouch */)\n',
    'flow_engine_keyword_trigger_multitouch');

-- ===========================================================================
-- 6. El permiso de la palabra clave, la compra como salida y la admision.
-- ===========================================================================

-- Por que una persona que escribio la palabra clave de una regla no entra; null si entra.
-- Solo la admision la llama (antes de crear nada). La baja la mira despues
-- _flow_engine_admit_native_flow, como para todo flujo.
create function public._flow_engine_native_keyword_reason(
    p_definition jsonb, p_tenant_ref text, p_product_ref text, p_contact_id uuid, p_destination text)
returns text
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    -- flow_engine_keyword_trigger_reason (20261127400000)
    -- La conversacion derivada: en manos de alguien del equipo, pausada,
    -- bloqueada o con la automatizacion frenada (el criterio de la audiencia).
    if exists (
        select 1 from public.conversations conversation
         where conversation.contact_id = p_contact_id
           and (conversation.human_takeover
                or conversation.status in ('paused_human', 'blocked')
                or conversation.automation_status in ('paused', 'disabled', 'restricted', 'error'))) then
        return 'conversacion_derivada';
    end if;
    -- La compra como salida: a quien ya compro el producto no se le escribe.
    if coalesce(jsonb_typeof(p_definition #> '{termina,si}') = 'array'
                and p_definition #> '{termina,si}' @> '["compra"]'::jsonb, false)
       and public._flow_engine_native_audience_purchased(p_tenant_ref, p_product_ref, p_contact_id,
                                                          p_destination, null) then
        return 'compra';
    end if;
    return null;
end;
$function$;

-- La admision de una conversacion cuyo primer mensaje trajo la palabra clave de una regla publicada.
create function public._flow_engine_admit_native_keyword_v1(p_instance jsonb, p_flow_event jsonb)
returns table(flow_id uuid, outcome text, reason text, recovery_case_id uuid, followup_sequence_id uuid,
              scheduled_action_id uuid, created boolean)
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_instance record;
    v_rule_id uuid;
    v_rule_key text;
    v_moment timestamptz;
    v_account_id bigint;
    v_inbox_id bigint;
    v_conversation bigint;
    v_message bigint;
    v_destination text;
    v_external text;
    v_flow_ids uuid[];
    v_flow_id uuid;
    v_contact_id uuid;
    v_control public.flow_controls%rowtype;
    v_event public.webhook_events%rowtype;
    v_one record;
    v_sqlstate text;
begin
    -- flow_engine_keyword_trigger_admission (20261127400000)
    v_instance := public._flow_store_instance(p_instance);
    if jsonb_typeof(p_flow_event) is distinct from 'object'
       or (select array_agg(k order by k) from jsonb_object_keys(p_flow_event) k)
          is distinct from array['chatwoot', 'destino', 'evento_id', 'fuente', 'momento', 'regla_clave', 'regla_id']
       or p_flow_event -> 'fuente' is distinct from '"palabra_clave"'::jsonb
       or jsonb_typeof(p_flow_event -> 'regla_id') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'regla_id' ~ c_uuid, false)
       or jsonb_typeof(p_flow_event -> 'regla_clave') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'regla_clave' ~ '^[a-z0-9][a-z0-9-]{0,63}$', false)
       or jsonb_typeof(p_flow_event -> 'momento') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'momento' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$', false)
       or jsonb_typeof(p_flow_event -> 'chatwoot') is distinct from 'object'
       or (select array_agg(k order by k) from jsonb_object_keys(
               case when jsonb_typeof(p_flow_event -> 'chatwoot') = 'object'
                    then p_flow_event -> 'chatwoot' else '{}'::jsonb end) k)
          is distinct from array['account_id', 'conversation_id', 'inbox_id', 'message_id']
       or jsonb_typeof(p_flow_event #> '{chatwoot,account_id}') is distinct from 'number'
       or jsonb_typeof(p_flow_event #> '{chatwoot,inbox_id}') is distinct from 'number'
       or jsonb_typeof(p_flow_event #> '{chatwoot,conversation_id}') is distinct from 'number'
       or jsonb_typeof(p_flow_event #> '{chatwoot,message_id}') is distinct from 'number'
       or not coalesce(p_flow_event #>> '{chatwoot,account_id}' ~ '^[1-9][0-9]{0,17}$', false)
       or not coalesce(p_flow_event #>> '{chatwoot,inbox_id}' ~ '^[1-9][0-9]{0,17}$', false)
       or not coalesce(p_flow_event #>> '{chatwoot,conversation_id}' ~ '^[1-9][0-9]{0,17}$', false)
       or not coalesce(p_flow_event #>> '{chatwoot,message_id}' ~ '^[1-9][0-9]{0,17}$', false)
       or p_flow_event ->> 'evento_id' is distinct from
          'palabra_clave:' || (p_flow_event #>> '{chatwoot,conversation_id}') || ':'
          || (p_flow_event #>> '{chatwoot,message_id}')
       or jsonb_typeof(p_flow_event -> 'destino') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'destino' ~ '^[0-9]{6,20}$', false) then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;
    v_rule_id := (p_flow_event ->> 'regla_id')::uuid;
    v_rule_key := p_flow_event ->> 'regla_clave';
    v_account_id := (p_flow_event #>> '{chatwoot,account_id}')::bigint;
    v_inbox_id := (p_flow_event #>> '{chatwoot,inbox_id}')::bigint;
    v_conversation := (p_flow_event #>> '{chatwoot,conversation_id}')::bigint;
    v_message := (p_flow_event #>> '{chatwoot,message_id}')::bigint;
    v_destination := p_flow_event ->> 'destino';
    begin
        v_moment := (p_flow_event ->> 'momento')::timestamptz;
    exception when others then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end;
    if v_moment > clock_timestamp() + interval '5 minutes' then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;

    -- La regla: publicada, de la instancia y con esa clave. Pausada o
    -- descartada entre el turno y la admision, no arranca nada y no deja fila.
    if not exists (
        select 1 from public.keyword_rules rule_row
         where rule_row.id = v_rule_id
           and rule_row.tenant_ref = v_instance.o_tenant_ref and rule_row.ally_ref = v_instance.o_ally_ref
           and rule_row.rule_key = v_rule_key and rule_row.status = 'publicada') then
        return query select null::uuid, 'sin_flujo'::text, null::text, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- Los flujos de la regla: de la instancia, con ruta nativa, no apagados,
    -- con la version activa disparada por esa clave y de un producto atado. La
    -- ruta y el control FOR SHARE, en orden de flow_key (D-Z2.5), antes del
    -- evento y del contacto (el orden de §2.4).
    select coalesce(array_agg(c.flow_id order by c.flow_key, c.flow_id), array[]::uuid[]) into v_flow_ids
      from public.flow_controls c
      join public.flow_engine_routes r on r.flow_id = c.flow_id and r.legacy_kind is null
      join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.state <> 'apagado'
       and public._flow_engine_native_trigger(d.definition) = 'palabra_clave'
       and d.definition #>> '{dispara,palabra_clave}' = v_rule_key
       and public._flow_store_product_is_bound(v_instance.o_tenant_ref, v_instance.o_ally_ref, c.product_ref);
    if cardinality(v_flow_ids) = 0 then
        return query select null::uuid, 'sin_flujo'::text, null::text, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    perform 1 from public.flow_engine_routes r where r.flow_id = any(v_flow_ids) order by r.flow_id for share;
    perform 1 from public.flow_controls c where c.flow_id = any(v_flow_ids) order by c.flow_id for share;

    -- El evento persistido: uno por mensaje de la conversacion. Sin telefono.
    v_external := 'keyword:' || v_rule_id::text || ':' || v_conversation::text || ':' || v_message::text;
    insert into public.webhook_events (source, external_event_id, event_type, payload, processing_status,
                                       processed_at)
    values ('system', v_external, 'FLOW_KEYWORD_MATCHED',
            jsonb_build_object('regla_id', v_rule_id, 'regla_clave', v_rule_key,
                               'chatwoot_conversation_id', v_conversation, 'chatwoot_message_id', v_message),
            'processed', clock_timestamp())
    on conflict (source, external_event_id) do nothing;
    select event_row.* into strict v_event from public.webhook_events event_row
     where event_row.source = 'system' and event_row.external_event_id = v_external
       for update;
    if v_event.event_type is distinct from 'FLOW_KEYWORD_MATCHED' then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;

    -- El contacto, desde Chatwoot: el de la identidad de WhatsApp activa del
    -- destino en esa cuenta, la que dejo la admision de la conversacion
    -- entrante (D-Z2.6). Sin ella no se crea nada.
    select identity_row.contact_id into v_contact_id
      from public.channel_identities identity_row
     where identity_row.channel = 'whatsapp' and identity_row.account_id = 'chatwoot:' || v_account_id::text
       and identity_row.external_user_id = v_destination and identity_row.identity_status = 'active';
    if v_contact_id is not null then
        perform 1 from public.contacts contact_row where contact_row.id = v_contact_id for update;
    end if;

    -- Cada flujo en su subtransaccion: la falla de uno no frena a los otros.
    foreach v_flow_id in array v_flow_ids loop
        select c.* into strict v_control from public.flow_controls c where c.flow_id = v_flow_id;
        if v_contact_id is null then
            perform public._flow_engine_record_start_rejection(
                v_control.flow_id, v_control.active_version, v_event.id, 'admision', 'no_entra:sin_contacto');
            select 'motor_no_arranca'::text as outcome, 'no_entra:sin_contacto'::text as reason,
                   null::uuid as recovery_case_id, null::uuid as followup_sequence_id,
                   null::uuid as scheduled_action_id, null::boolean as created
              into v_one;
        else
            begin
                select one.* into v_one
                  from public._flow_engine_admit_native_flow(
                      v_control, v_event, v_contact_id, null::text, v_moment, v_account_id, v_inbox_id,
                      v_destination) one;
                -- Un recorrido nuevo corta los de su le_gana_a, solo si el bridge lo pide.
                if p_instance -> 'native_priority_wins' = 'true'::jsonb
                   and v_one.outcome = 'motor_arranco' and v_one.created is true then
                    perform public._flow_engine_native_priority_wins_v1(
                        v_one.recovery_case_id, v_event.id, clock_timestamp());
                end if;
            exception when others then
                get stacked diagnostics v_sqlstate = returned_sqlstate;
                if v_sqlstate = '23514' and sqlerrm = 'channel_identity_contact_mismatch' then
                    perform public._flow_engine_record_start_rejection(
                        v_control.flow_id, v_control.active_version, v_event.id, 'admision',
                        'no_entra:identidad_de_otro_contacto');
                    select 'motor_no_arranca'::text as outcome, 'no_entra:identidad_de_otro_contacto'::text as reason,
                           null::uuid as recovery_case_id, null::uuid as followup_sequence_id,
                           null::uuid as scheduled_action_id, null::boolean as created
                      into v_one;
                else
                    perform public._flow_engine_record_start_rejection(
                        v_control.flow_id, v_control.active_version, v_event.id, 'admision',
                        'error_interno:' || lower(v_sqlstate));
                    select 'motor_no_arranca'::text as outcome, 'error_interno:' || lower(v_sqlstate) as reason,
                           null::uuid as recovery_case_id, null::uuid as followup_sequence_id,
                           null::uuid as scheduled_action_id, null::boolean as created
                      into v_one;
                end if;
            end;
        end if;
        return query select v_control.flow_id, v_one.outcome, v_one.reason, v_one.recovery_case_id,
            v_one.followup_sequence_id, v_one.scheduled_action_id, v_one.created;
    end loop;
end;
$function$;

-- 6b. _flow_engine_admit_native_flow: el permiso, el caso, el context y la autorizacion.
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'    -- e483_native_audience_permission: end\n',
    E'    -- e483_native_audience_permission: end\n'
    || E'    -- flow_engine_keyword_trigger_permission: begin (20261127400000)\n'
    || E'    -- La palabra clave: la persona abrio la conversacion con el link; la\n'
    || E'    -- conversacion derivada y la compra (_flow_engine_native_keyword_reason).\n'
    || E'    elsif public._flow_engine_native_trigger(v_definition.definition) = ''palabra_clave''\n'
    || E'       and p_event.source = ''system'' and p_event.event_type = ''FLOW_KEYWORD_MATCHED'' then\n'
    || E'        v_reason := public._flow_engine_native_keyword_reason(\n'
    || E'            v_definition.definition, p_control.tenant_ref, p_control.product_ref, p_contact_id, p_destination);\n'
    || E'    -- flow_engine_keyword_trigger_permission: end\n',
    'flow_engine_keyword_trigger_permission');
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'        case when public._flow_engine_native_trigger(v_definition.definition) in (''intencion'', ''audiencia'')\n'
    || E'             then ''landing'' else p_event.source end,\n',
    E'        -- flow_engine_keyword_trigger_source: y la palabra clave, que nace de un evento system (20261127400000).\n'
    || E'        case when public._flow_engine_native_trigger(v_definition.definition) in (''intencion'', ''audiencia'', ''palabra_clave'')\n'
    || E'             then ''landing'' else p_event.source end,\n',
    'flow_engine_keyword_trigger_source');
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'                then jsonb_build_object(''flow_audience_occurrence_id'', p_event.payload ->> ''ocurrencia_id'',\n'
    || E'                                        ''flow_audience_form'', p_event.payload ->> ''tipo'')\n'
    || E'                else ''{}''::jsonb end,\n',
    E'                then jsonb_build_object(''flow_audience_occurrence_id'', p_event.payload ->> ''ocurrencia_id'',\n'
    || E'                                        ''flow_audience_form'', p_event.payload ->> ''tipo'')\n'
    || E'                else ''{}''::jsonb end\n'
    || E'        -- flow_engine_keyword_trigger_context: la regla y la conversacion (20261127400000).\n'
    || E'        || case when public._flow_engine_native_trigger(v_definition.definition) = ''palabra_clave''\n'
    || E'                then jsonb_build_object(''keyword_rule_id'', p_event.payload ->> ''regla_id'',\n'
    || E'                                        ''keyword_rule_key'', p_event.payload ->> ''regla_clave'',\n'
    || E'                                        ''chatwoot_conversation_id'', p_event.payload -> ''chatwoot_conversation_id'',\n'
    || E'                                        ''flow_engine_product_ref'', p_control.product_ref)\n'
    || E'                else ''{}''::jsonb end,\n',
    'flow_engine_keyword_trigger_context');
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'                      and public._flow_engine_native_trigger(v_definition.definition) is distinct from ''audiencia''\n',
    E'                      and public._flow_engine_native_trigger(v_definition.definition) is distinct from ''audiencia''\n'
    || E'                      -- flow_engine_keyword_trigger_grant_source: y la palabra clave (20261127400000).\n'
    || E'                      and public._flow_engine_native_trigger(v_definition.definition) is distinct from ''palabra_clave''\n',
    'flow_engine_keyword_trigger_grant_source');
select pg_temp.flow_engine_keyword_trigger_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'                 -- e483_native_audience_grant: la lista de la ocurrencia (20261112210600).\n',
    E'                 -- flow_engine_keyword_trigger_grant: la regla y el mensaje de la persona (20261127400000).\n'
    || E'                 when public._flow_engine_native_trigger(v_definition.definition) = ''palabra_clave''\n'
    || E'                 then jsonb_build_object(''reason'', ''keyword_rule'', ''webhook_event_id'', p_event.id,\n'
    || E'                                         ''keyword_rule_id'', p_event.payload ->> ''regla_id'',\n'
    || E'                                         ''chatwoot_conversation_id'', p_event.payload -> ''chatwoot_conversation_id'',\n'
    || E'                                         ''chatwoot_message_id'', p_event.payload -> ''chatwoot_message_id'',\n'
    || E'                                         ''recovery_case_id'', v_case_id)\n'
    || E'                 -- e483_native_audience_grant: la lista de la ocurrencia (20261112210600).\n',
    'flow_engine_keyword_trigger_grant:');

-- ===========================================================================
-- 7. El despacho: el producto del recorrido y el numero de la identidad.
-- ===========================================================================
select pg_temp.flow_engine_keyword_trigger_patch(
    'public.get_followup_execution_context(uuid,text,bigint,timestamptz)',
    E'        c.phone,\n',
    E'        -- flow_engine_keyword_trigger_phone: un contacto de una conversacion entrante\n'
    || E'        -- no tiene telefono; el de su identidad de WhatsApp, la del caso (20261127400000).\n'
    || E'        case when c.phone is null and rc.policy_key like ''flujo:%''\n'
    || E'                  and rc.context ->> ''flow_engine_trigger'' = ''palabra_clave''\n'
    || E'             then (select ci.external_user_id from public.channel_identities ci\n'
    || E'                    where ci.id = rc.selected_channel_identity_id and ci.contact_id = rc.contact_id\n'
    || E'                      and ci.channel = ''whatsapp'')\n'
    || E'             else c.phone end,\n',
    'flow_engine_keyword_trigger_phone');
select pg_temp.flow_engine_keyword_trigger_patch(
    'public.get_followup_execution_context(uuid,text,bigint,timestamptz)',
    E'                      and we.event_type = ''FLOW_AUDIENCE_RELEASED'')\n',
    E'                      and we.event_type = ''FLOW_AUDIENCE_RELEASED'')\n'
    || E'             -- flow_engine_keyword_trigger_product: la palabra clave nace sin oferta; el\n'
    || E'             -- producto de su flujo, que la admision deja en el context: service_role\n'
    || E'             -- no lee flow_engine_runs (20261127400000).\n'
    || E'             when rc.offer_code is null and rc.policy_key like ''flujo:%''\n'
    || E'                  and rc.context ->> ''flow_engine_trigger'' = ''palabra_clave''\n'
    || E'             then rc.context ->> ''flow_engine_product_ref''\n',
    'flow_engine_keyword_trigger_product');

-- ===========================================================================
-- 8. La compra como salida, en la reevaluacion.
-- ===========================================================================
create function public._flow_engine_native_keyword_purchase_stop(p_case_id uuid)
returns text
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.recovery_cases%rowtype;
    v_definition jsonb;
    v_tenant text;
    v_product text;
    v_destination text;
begin
    -- flow_engine_keyword_trigger_purchase_stop (20261127400000)
    select rc.* into v_case from public.recovery_cases rc where rc.id = p_case_id;
    select d.definition, c.tenant_ref, c.product_ref into v_definition, v_tenant, v_product
      from public.flow_engine_runs run
      join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id
      join public.flow_definitions d on d.flow_id = run.flow_id and d.version = run.version
      join public.flow_controls c on c.flow_id = run.flow_id
     where run.recovery_case_id = p_case_id and native.trigger_event = 'palabra_clave';
    if v_case.id is null or v_definition is null
       or not coalesce(jsonb_typeof(v_definition #> '{termina,si}') = 'array'
                       and v_definition #> '{termina,si}' @> '["compra"]'::jsonb, false) then
        return null;
    end if;
    select identity_row.external_user_id into v_destination
      from public.channel_identities identity_row
     where identity_row.id = v_case.selected_channel_identity_id and identity_row.contact_id = v_case.contact_id;
    if public._flow_engine_native_audience_purchased(v_tenant, v_product, v_case.contact_id, v_destination, null) then
        return 'purchase_detected_any_offer';
    end if;
    return null;
end;
$function$;

select pg_temp.flow_engine_keyword_trigger_patch(
    'public._portable_case_purchase_stop_reason(uuid)',
    E'    if v_case.context ->> ''flow_engine_trigger'' = ''audiencia'' then\n'
    || E'        return public._flow_engine_native_audience_purchase_stop(p_case_id);\n'
    || E'    end if;\n',
    E'    if v_case.context ->> ''flow_engine_trigger'' = ''audiencia'' then\n'
    || E'        return public._flow_engine_native_audience_purchase_stop(p_case_id);\n'
    || E'    end if;\n'
    || E'    -- flow_engine_keyword_trigger_case: y uno de palabra clave, por su flujo (20261127400000).\n'
    || E'    if v_case.context ->> ''flow_engine_trigger'' = ''palabra_clave'' then\n'
    || E'        return public._flow_engine_native_keyword_purchase_stop(p_case_id);\n'
    || E'    end if;\n',
    'flow_engine_keyword_trigger_case');

-- ===========================================================================
-- 1. La entrada: solo con la clave del bridge y un evento de palabra clave.
-- ===========================================================================
select pg_temp.flow_engine_keyword_trigger_patch(
    'public.admit_flow_engine_event_v1(jsonb,jsonb)',
    E'    -- e483_native_audience_admit: end\n',
    E'    -- e483_native_audience_admit: end\n\n'
    || E'    -- flow_engine_keyword_trigger_admit: begin (20261127400000)\n'
    || E'    -- La conversacion cuyo primer mensaje trajo la palabra clave de una regla,\n'
    || E'    -- solo si el bridge lo pide (KEYWORD_FLOW_TRIGGER_ENABLED).\n'
    || E'    if p_instance -> ''native_keyword'' = ''true''::jsonb\n'
    || E'       and jsonb_typeof(p_flow_event) = ''object''\n'
    || E'       and p_flow_event -> ''fuente'' = ''"palabra_clave"''::jsonb then\n'
    || E'        return query select keyword.* from public._flow_engine_admit_native_keyword_v1(\n'
    || E'            p_instance, p_flow_event) keyword;\n'
    || E'        return;\n'
    || E'    end if;\n'
    || E'    -- flow_engine_keyword_trigger_admit: end\n',
    'flow_engine_keyword_trigger_admit');

-- ===========================================================================
-- Los permisos: las funciones nuevas no son de la API.
-- ===========================================================================
do $acl$
declare
    v_signature text;
    v_role text;
begin
    foreach v_signature in array array[
        'public._flow_engine_admit_native_keyword_v1(jsonb,jsonb)',
        'public._flow_engine_native_keyword_reason(jsonb,text,text,uuid,text)',
        'public._flow_engine_native_keyword_purchase_stop(uuid)'
    ] loop
        execute format('revoke all on function %s from public', v_signature);
        foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
            if to_regrole(v_role) is not null then
                execute format('revoke all on function %s from %I', v_signature, v_role);
                if has_function_privilege(v_role, v_signature, 'EXECUTE') then
                    raise exception using errcode = '55000', message = 'flow_engine_keyword_trigger_acl_unexpected',
                        detail = v_signature || ' ' || v_role;
                end if;
            end if;
        end loop;
    end loop;
end;
$acl$;

do $check$
begin
    if public._flow_engine_native_trigger('{"dispara": {"palabra_clave": "soporte"}}'::jsonb) is distinct from 'palabra_clave'
       or public._flow_engine_native_trigger('{"dispara": {"palabra_clave": "soporte", "evento": "compra"}}'::jsonb) is not null
       or public._flow_engine_native_trigger('{"dispara": {"palabra_clave": {}}}'::jsonb) is not null
       or public._flow_engine_native_trigger('{"dispara": {"evento": "compra"}}'::jsonb) is distinct from 'compra'
       or public._flow_engine_native_trigger('{"dispara": {"manual": {}}}'::jsonb) is distinct from 'audiencia'
       or jsonb_array_length(public._flow_engine_native_steps('nativo:palabra_clave')) <> 3
       or position('flow_engine_keyword_trigger_admit: end' in pg_get_functiondef(
              'public.admit_flow_engine_event_v1(jsonb,jsonb)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_keyword_trigger_not_applied';
    end if;
end;
$check$;

-- ===========================================================================
-- 9. La fila del recorrido. Al final: ACCESS EXCLUSIVE lo menos posible.
-- ===========================================================================
alter table public.flow_engine_native_runs
    drop constraint flow_engine_native_runs_trigger_event_check,
    add constraint flow_engine_native_runs_trigger_event_check
        -- e483_native_audience: y la audiencia (20261112210600).
        -- e528_native_payment_slip: y la ficha impresa (20261113200000).
        -- flow_engine_keyword_trigger: y la palabra clave (20261127400000).
        check (trigger_event in ('compra', 'carrito', 'pago_fallido', 'intencion', 'audiencia', 'ficha_impresa',
                                 'palabra_clave'));

commit;
