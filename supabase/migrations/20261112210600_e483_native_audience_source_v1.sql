-- e483-native-audience-source-v1: la admision nativa acepta el evento de un
-- disparo por audiencia (E-483, para E-484: los disparadores fecha, cada y
-- manual, H6 del mapa «el AE maneja todos los flujos», 2026-11-12).
--
-- Por que. E-484 arma, por cada ocurrencia de un flujo con dispara.fecha,
-- dispara.cada o dispara.manual, una lista congelada de personas y la suelta al
-- ritmo por hora. Cada persona tiene que entrar a la puerta comun del motor
-- nativo (la baja, la repeticion, la bolsa, el horario, la compra como salida),
-- y la admision solo aceptaba un evento de Hotmart (fuente: hotmart) que elige
-- los flujos por su disparador. Una persona de una lista no es un evento de
-- Hotmart: ya viene atada a un flujo y a su version.
--
-- Que cambia:
--   1. admit_flow_engine_event_v1: solo con p_instance.native_audience = true (el
--      bridge la manda con FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED) y un evento con
--      fuente: audiencia, la admision va a la funcion nueva del punto 2. Sin la
--      clave, o con cualquier otra fuente, el cuerpo de antes: un evento de
--      audiencia sin la clave sigue siendo invalid_flow_engine_event (22023).
--   2. _flow_engine_admit_native_audience_v1 (nueva, privada): la forma exacta
--      del evento (tipo, evento_id = audiencia:<ocurrencia>:<contacto>, flujo_id,
--      version, ocurrencia_id, producto_ref, contacto_id, momento, chatwoot,
--      destino); el flujo de ESE flujo_id, de la instancia y del producto, con
--      ruta nativa, prendido, en cohorte o pausado, con ESA version activa y un
--      disparo por audiencia de ese tipo (si no: sin_flujo, sin fila). El evento
--      persistido es uno sintetico de webhook_events (source system,
--      FLOW_AUDIENCE_RELEASED, external_event_id
--      flow_audience:<flujo>:<version>:<ocurrencia>:<contacto>): es lo que hace la
--      admision idempotente por (flujo, version, ocurrencia, contacto), con la
--      repeticion del mismo evento de _flow_engine_admit_native_flow. Despues, esa
--      funcion, como cualquier evento: la cohorte, la baja, la repeticion de la
--      definicion, no_arranca_mientras, la bolsa, la politica y el recorrido.
--   3. _flow_engine_native_trigger: audiencia, si dispara es exactamente una de
--      fecha, cada o manual, con un objeto.
--   4. _flow_engine_native_steps: nativo:audiencia, hasta tres toques con nombre y
--      producto y sin cupon (la forma del formulario).
--   5. _flow_engine_native_unsupported: la audiencia entra con permiso
--      formulario (prospectas) o compra (clientas), y puede tener varios toques
--      con las reglas de siempre (contesta escrita, el boton solo en el primero,
--      sin condicion). La base no tiene la instancia aca (la funcion es inmutable
--      y la llaman la publicacion y la apertura): como con 20261112210000, la
--      base lo abre y lo que no deja publicar con el flag apagado es el
--      validador del bridge (motor_nativo:disparador, NativeRuntime).
--   6. _flow_engine_admit_native_flow: el permiso de la audiencia
--      (_flow_engine_native_audience_reason, nueva: el formulario con permiso de
--      WhatsApp del producto en la ventana para.formulario_en, 30 dias por
--      defecto, el criterio de E-484; o una compra aprobada del producto de la
--      persona; la conversacion derivada; una respuesta de la persona en las
--      ultimas 24 h; y con la salida compra, una compra del producto), el caso
--      landing (la check de recovery_cases.source no admite system) con la
--      ocurrencia en el context, la autorizacion con la evidencia flow_audience y
--      el recorrido con trigger_event audiencia.
--   7. (Sin cambio) La puerta antes de cada envio ya mira, para cualquier
--      recorrido nativo, la baja del contacto y la de Chatwoot de ese movil
--      (mark_flow_engine_request_started_v1): la prueba lo mide con la audiencia.
--   8. _portable_case_purchase_stop_reason: un caso de audiencia con la salida
--      compra se corta con una compra del producto de la persona
--      (purchase_detected_any_offer, que la reevaluacion ya cierra); con permiso
--      compra, solo una compra posterior al caso. Un caso que no es de audiencia,
--      el cuerpo de antes.
--   9. flow_engine_native_runs: trigger_event admite audiencia.
--
-- Lo que NO cambia. Ningun flujo publicado tiene un disparo por audiencia (el
-- esquema no lo escribia), asi que la forma, la apertura y la admision de todo
-- lo que corre en ATT1 y Maribel son las de antes. Sin la clave en p_instance la
-- admision es la de antes byte a byte para cualquier evento.
--
-- Como: el metodo de 20261112210000, reemplazos contados sobre la definicion
-- VIGENTE de cada funcion, cada uno con su marca, que no puede estar. Correrla
-- dos veces aborta con 55000. El bloque 9 toma ACCESS EXCLUSIVE sobre
-- flow_engine_native_runs: va al final, con lock_timeout.
--
-- Errores: 22023 entrada invalida; P0002 el contacto no existe; 55000 un estado
-- que no lo permite.

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
       or to_regprocedure('public._portable_checkout_link_purchase_exists(text,text,uuid,text,text)') is null
       or to_regprocedure('public._whatsapp_phone_variants(text)') is null
       or to_regprocedure('public._flow_store_product_is_bound(text,text,text)') is null
       or position('e483_payment_failure_by_phone_permission: end' in pg_get_functiondef(
              'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_native_audience_missing';
    end if;
    if to_regprocedure('public._flow_engine_admit_native_audience_v1(jsonb,jsonb)') is not null
       or position('e483_native_audience' in pg_get_functiondef(
              'public.admit_flow_engine_event_v1(jsonb,jsonb)'::regprocedure)) > 0 then
        raise exception using errcode = '55000', message = 'e483_native_audience_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla: el ancla una sola vez, la marca ninguna; despues, la marca.
create function pg_temp.e483_native_audience_patch(
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
        raise exception using errcode = '55000', message = 'e483_native_audience_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_e483_native_audience_definition',
            detail = p_signature || ' ' || p_marker;
    end if;
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = p_signature::regprocedure;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_native_audience_not_replaced',
            detail = p_signature || ' ' || p_marker;
    end if;
    -- create or replace conserva el duenio y los permisos: se verifica.
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from v_acl then
        raise exception using errcode = '55000', message = 'e483_native_audience_acl_changed',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- 3. El disparador.
-- ===========================================================================
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_native_trigger(jsonb)',
    E'        when p_definition -> ''dispara'' = ''{"evento": "compra"}''::jsonb then ''compra''\n',
    E'        -- e483_native_audience_trigger: el disparo por audiencia, exactamente una de fecha,\n'
    || E'        -- cada o manual con un objeto (20261112210600).\n'
    || E'        when jsonb_typeof(p_definition -> ''dispara'') = ''object''\n'
    || E'             and (((p_definition -> ''dispara'') - ''fecha'' = ''{}''::jsonb\n'
    || E'                   and jsonb_typeof(p_definition #> ''{dispara,fecha}'') = ''object'')\n'
    || E'                  or ((p_definition -> ''dispara'') - ''cada'' = ''{}''::jsonb\n'
    || E'                      and jsonb_typeof(p_definition #> ''{dispara,cada}'') = ''object'')\n'
    || E'                  or ((p_definition -> ''dispara'') - ''manual'' = ''{}''::jsonb\n'
    || E'                      and jsonb_typeof(p_definition #> ''{dispara,manual}'') = ''object'')) then ''audiencia''\n'
    || E'        when p_definition -> ''dispara'' = ''{"evento": "compra"}''::jsonb then ''compra''\n',
    'e483_native_audience_trigger');

-- ===========================================================================
-- 4. La forma: hasta tres toques, con nombre y producto y sin cupon.
-- ===========================================================================
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_native_steps(text)',
    E'        -- flow_engine_native_intent: el primer contacto del formulario, de un toque (20261101000100).\n',
    E'        -- e483_native_audience_steps: el disparo por audiencia, hasta tres toques sin cupon (20261112210600).\n'
    || E'        when ''nativo:audiencia'' then\n'
    || E'            ''[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
    || '"variables_de": ["nombre", "producto"], "coupon_variables": null}, '
    || '{"step_key": "native_touch_2", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
    || '"variables_de": ["nombre", "producto"], "coupon_variables": null}, '
    || '{"step_key": "native_touch_3", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
    || E'"variables_de": ["nombre", "producto"], "coupon_variables": null}]''::jsonb\n'
    || E'        -- flow_engine_native_intent: el primer contacto del formulario, de un toque (20261101000100).\n',
    'e483_native_audience_steps');

-- ===========================================================================
-- 5. Lo que abre la base: el permiso y los toques.
-- ===========================================================================
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_native_unsupported(jsonb)',
    E'                  when ''silencio_agente'' then ''"conversacion"''\n',
    E'                  when ''silencio_agente'' then ''"conversacion"''\n'
    || E'                  -- e483_native_audience_permission: formulario (prospectas) o compra (clientas) (20261112210600).\n'
    || E'                  when ''audiencia'' then case when p_definition #> ''{entra,permiso}'' = ''"compra"''::jsonb\n'
    || E'                                             then ''"compra"'' else ''"formulario"'' end\n',
    'e483_native_audience_permission');
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_native_unsupported(jsonb)',
    E'not in (''carrito'', ''intencion'', ''pago_fallido'')\n',
    E'not in (''carrito'', ''intencion'', ''pago_fallido'', ''audiencia'' /* e483_native_audience_multitouch */)\n',
    'e483_native_audience_multitouch');

-- ===========================================================================
-- 6. El permiso de la audiencia, la compra de la persona y la admision.
-- ===========================================================================

-- Si la persona compro el producto: una compra aprobada admitida del binding con
-- su identidad (el telefono en sus dos formas, o un mail o un telefono del
-- contacto), o una compra con nuestro link. Con p_since, solo las de despues.
create function public._flow_engine_native_audience_purchased(
    p_tenant_ref text, p_funnel_ref text, p_contact_id uuid, p_destination text, p_since timestamptz)
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    -- e483_native_audience_purchased (20261112210600)
    select exists (
               select 1
                 from public.portable_hotmart_purchase_correlations purchase
                 join public.hotmart_purchase_intent_event_identities buyer
                   on buyer.webhook_event_id = purchase.webhook_event_id
                where purchase.tenant_ref = p_tenant_ref
                  and purchase.funnel_ref = p_funnel_ref
                  and (p_since is null or purchase.observed_at > p_since)
                  -- Sin reembolso ni contracargo posterior de la misma transaccion (el criterio de
                  -- _flow_audience_purchases de 20261112220000 y de get_customer_status_v1).
                  and not exists (
                      select 1
                        from public.webhook_events approved
                        join public.webhook_events reversal
                          on reversal.source = 'hotmart'
                         and reversal.event_type in ('PURCHASE_REFUNDED', 'PURCHASE_CHARGEBACK', 'PURCHASE_PROTEST')
                         and reversal.payload #>> '{data,purchase,transaction}'
                             = approved.payload #>> '{data,purchase,transaction}'
                       where approved.id = purchase.webhook_event_id
                         and nullif(approved.payload #>> '{data,purchase,transaction}', '') is not null)
                  and ((buyer.normalized_phone is not null
                        and buyer.normalized_phone = any(public._whatsapp_phone_variants(p_destination)))
                       or exists (
                           select 1 from public.contact_points point
                            where point.contact_id = p_contact_id
                              and ((point.type = 'email' and point.normalized_value = buyer.normalized_email)
                                   or (point.type = 'phone' and buyer.normalized_phone is not null
                                       and point.normalized_value = any(
                                           public._whatsapp_phone_variants(buyer.normalized_phone)))))))
        or (p_since is null
            and public._portable_checkout_link_purchase_exists(p_tenant_ref, p_funnel_ref, p_contact_id,
                                                                null, p_destination))
$function$;

-- Por que una persona de una lista no entra a un flujo de audiencia; null si entra.
-- Solo la admision la llama (antes de crear nada).
create function public._flow_engine_native_audience_reason(
    p_definition jsonb, p_tenant_ref text, p_product_ref text, p_contact_id uuid, p_destination text)
returns text
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_permission text := p_definition #>> '{entra,permiso}';
    v_days integer;
begin
    -- e483_native_audience_reason (20261112210600)
    -- El permiso. Formulario: el formulario del producto con permiso de WhatsApp
    -- en la ventana para.formulario_en (30 dias por defecto), el criterio de la
    -- lista de E-484. Compra: una compra aprobada del producto de la persona.
    if v_permission = 'formulario' then
        -- La regla de la lista (20261112220000) y del validador: de 1d a 365d, 30d sin la clave.
        if coalesce(p_definition #>> '{para,formulario_en}', '30d') ~ '^[1-9][0-9]{0,2}d$' then
            v_days := left(coalesce(p_definition #>> '{para,formulario_en}', '30d'), -1)::integer;
        end if;
        if v_days is null or v_days > 365 then
            return 'no_entra:permiso';
        end if;
        if not exists (
            select 1 from public.purchase_intents intent
             where intent.tenant_ref = p_tenant_ref and intent.funnel_ref = p_product_ref
               and intent.normalized_phone = any(public._whatsapp_phone_variants(p_destination))
               and intent.whatsapp_contact_authorized
               and intent.submitted_at >= clock_timestamp() - make_interval(days => v_days)) then
            return 'no_entra:permiso';
        end if;
    elsif v_permission = 'compra' then
        if not public._flow_engine_native_audience_purchased(p_tenant_ref, p_product_ref, p_contact_id,
                                                              p_destination, null) then
            return 'no_entra:permiso';
        end if;
    else
        return 'no_entra:permiso';
    end if;
    -- La conversacion derivada: en manos de alguien del equipo, pausada,
    -- bloqueada o con la automatizacion frenada (el criterio de
    -- _portable_precheckout_stop_reason, sin closed: una conversacion vieja
    -- resuelta no frena un envio a una audiencia).
    if exists (
        select 1 from public.conversations conversation
         where conversation.contact_id = p_contact_id
           and (conversation.human_takeover
                or conversation.status in ('paused_human', 'blocked')
                or conversation.automation_status in ('paused', 'disabled', 'restricted', 'error'))) then
        return 'conversacion_derivada';
    end if;
    -- La conversacion viva: la persona nos escribio en las ultimas 24 h. La tabla
    -- messages del setter guarda solo salientes (Maribel, 13/11: 264 y 0
    -- entrantes) y conversations.last_inbound_at no se llena nunca: lo que el
    -- setter registra de un entrante es el turno del agente (agent_turn_provenance,
    -- por el numero de la conversacion de Chatwoot) y la adopcion de la
    -- conversacion (conversation_events inbound_*), la senal de
    -- recuperador_estado.py (E-511). Los numeros de Chatwoot de la persona salen de
    -- sus conversaciones, de sus adopciones y de sus casos por entrante. El
    -- entrante en messages queda por si algun camino lo guarda (E-531).
    if exists (
        select 1 from public.conversation_events event
          join public.conversations conversation on conversation.id = event.conversation_id
         where conversation.contact_id = p_contact_id and event.event_type like 'inbound%'
           and event.created_at > clock_timestamp() - interval '24 hours')
       or exists (
        select 1 from public.agent_turn_provenance turn
         where turn.occurred_at > clock_timestamp() - interval '24 hours'
           and turn.external_conversation_id::text in (
               select conversation.commercial_context ->> 'chatwoot_conversation_id'
                 from public.conversations conversation
                where conversation.contact_id = p_contact_id
               union
               select event.data ->> 'chatwoot_conversation_id'
                 from public.conversation_events event
                 join public.conversations conversation on conversation.id = event.conversation_id
                where conversation.contact_id = p_contact_id
               union
               select admission.external_conversation_id::text
                 from public.inbound_commercial_case_admissions admission
                where admission.contact_id = p_contact_id))
       or exists (
        select 1 from public.messages message
          join public.conversations conversation on conversation.id = message.conversation_id
         where conversation.contact_id = p_contact_id and message.direction = 'inbound'
           and message.occurred_at > clock_timestamp() - interval '24 hours') then
        return 'conversacion_viva';
    end if;
    -- La compra como salida: a una prospecta que ya compro no se le escribe.
    if v_permission = 'formulario'
       and coalesce(jsonb_typeof(p_definition #> '{termina,si}') = 'array'
                    and p_definition #> '{termina,si}' @> '["compra"]'::jsonb, false)
       and public._flow_engine_native_audience_purchased(p_tenant_ref, p_product_ref, p_contact_id,
                                                          p_destination, null) then
        return 'compra';
    end if;
    return null;
end;
$function$;

-- La admision de una persona de la lista de una ocurrencia, atada a su flujo y version.
create function public._flow_engine_admit_native_audience_v1(p_instance jsonb, p_flow_event jsonb)
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
    v_type text;
    v_flow_id uuid;
    v_version integer;
    v_occurrence uuid;
    v_contact_id uuid;
    v_product text;
    v_moment timestamptz;
    v_account_id bigint;
    v_inbox_id bigint;
    v_destination text;
    v_external text;
    v_control public.flow_controls%rowtype;
    v_event public.webhook_events%rowtype;
    v_one record;
    v_sqlstate text;
begin
    -- e483_native_audience_admission (20261112210600)
    v_instance := public._flow_store_instance(p_instance);
    if jsonb_typeof(p_flow_event) is distinct from 'object'
       or (select array_agg(k order by k) from jsonb_object_keys(p_flow_event) k)
          is distinct from array['chatwoot', 'contacto_id', 'destino', 'evento_id', 'flujo_id', 'fuente', 'momento',
                                 'ocurrencia_id', 'producto_ref', 'tipo', 'version']
       or p_flow_event -> 'fuente' is distinct from '"audiencia"'::jsonb
       or coalesce(p_flow_event ->> 'tipo', '') not in ('fecha', 'cada', 'manual')
       or jsonb_typeof(p_flow_event -> 'flujo_id') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'flujo_id' ~ c_uuid, false)
       or jsonb_typeof(p_flow_event -> 'ocurrencia_id') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'ocurrencia_id' ~ c_uuid, false)
       or jsonb_typeof(p_flow_event -> 'contacto_id') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'contacto_id' ~ c_uuid, false)
       or jsonb_typeof(p_flow_event -> 'version') is distinct from 'number'
       or not coalesce(p_flow_event ->> 'version' ~ '^[1-9][0-9]{0,8}$', false)
       or p_flow_event ->> 'evento_id' is distinct from
          'audiencia:' || (p_flow_event ->> 'ocurrencia_id') || ':' || (p_flow_event ->> 'contacto_id')
       or not coalesce(p_flow_event ->> 'producto_ref' ~ '^[a-z0-9][a-z0-9-]{0,127}$', false)
       or jsonb_typeof(p_flow_event -> 'momento') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'momento' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$', false)
       or jsonb_typeof(p_flow_event -> 'chatwoot') is distinct from 'object'
       or (select array_agg(k order by k) from jsonb_object_keys(
               case when jsonb_typeof(p_flow_event -> 'chatwoot') = 'object'
                    then p_flow_event -> 'chatwoot' else '{}'::jsonb end) k)
          is distinct from array['account_id', 'inbox_id']
       or jsonb_typeof(p_flow_event #> '{chatwoot,account_id}') is distinct from 'number'
       or jsonb_typeof(p_flow_event #> '{chatwoot,inbox_id}') is distinct from 'number'
       or not coalesce(p_flow_event #>> '{chatwoot,account_id}' ~ '^[1-9][0-9]{0,17}$', false)
       or not coalesce(p_flow_event #>> '{chatwoot,inbox_id}' ~ '^[1-9][0-9]{0,17}$', false)
       or jsonb_typeof(p_flow_event -> 'destino') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'destino' ~ '^[0-9]{6,20}$', false) then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;
    v_type := p_flow_event ->> 'tipo';
    v_flow_id := (p_flow_event ->> 'flujo_id')::uuid;
    v_version := (p_flow_event ->> 'version')::integer;
    v_occurrence := (p_flow_event ->> 'ocurrencia_id')::uuid;
    v_contact_id := (p_flow_event ->> 'contacto_id')::uuid;
    v_product := p_flow_event ->> 'producto_ref';
    v_account_id := (p_flow_event #>> '{chatwoot,account_id}')::bigint;
    v_inbox_id := (p_flow_event #>> '{chatwoot,inbox_id}')::bigint;
    v_destination := p_flow_event ->> 'destino';
    begin
        v_moment := (p_flow_event ->> 'momento')::timestamptz;
    exception when others then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end;
    if v_moment > clock_timestamp() + interval '5 minutes' then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;
    if not public._flow_store_product_is_bound(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_product) then
        raise exception using errcode = '22023', message = 'flow_store_product_not_bound';
    end if;

    -- El flujo del evento, y solo ese: de la instancia y del producto, con ruta
    -- nativa, no apagado, con ESA version activa y un disparo por audiencia de
    -- ese tipo. Otra version (el flujo cambio despues de armar la lista) o otro
    -- flujo no arrancan y no dejan fila.
    if not exists (
        select 1
          from public.flow_controls c
          join public.flow_engine_routes r on r.flow_id = c.flow_id and r.legacy_kind is null
          join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
         where c.flow_id = v_flow_id
           and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and c.product_ref = v_product and c.state <> 'apagado' and c.active_version = v_version
           and public._flow_engine_native_trigger(d.definition) = 'audiencia'
           and (d.definition -> 'dispara') ? v_type) then
        return query select null::uuid, 'sin_flujo'::text, null::text, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    -- La ruta y el control FOR SHARE, antes del evento y del contacto (el orden de §2.4).
    perform 1 from public.flow_engine_routes r where r.flow_id = v_flow_id for share;
    perform 1 from public.flow_controls c where c.flow_id = v_flow_id for share;
    select c.* into strict v_control from public.flow_controls c where c.flow_id = v_flow_id;
    if v_control.active_version is distinct from v_version or v_control.state = 'apagado' then
        return query select null::uuid, 'sin_flujo'::text, null::text, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- El evento persistido: uno por (flujo, version, ocurrencia, contacto).
    v_external := 'flow_audience:' || v_flow_id::text || ':' || v_version::text || ':'
                  || v_occurrence::text || ':' || v_contact_id::text;
    insert into public.webhook_events (source, external_event_id, event_type, payload, processing_status,
                                       processed_at)
    values ('system', v_external, 'FLOW_AUDIENCE_RELEASED',
            jsonb_build_object('flujo_id', v_flow_id, 'version', v_version, 'ocurrencia_id', v_occurrence,
                               'contacto_id', v_contact_id, 'tipo', v_type, 'producto_ref', v_product),
            'processed', clock_timestamp())
    on conflict (source, external_event_id) do nothing;
    select event_row.* into strict v_event from public.webhook_events event_row
     where event_row.source = 'system' and event_row.external_event_id = v_external
       for update;
    if v_event.event_type is distinct from 'FLOW_AUDIENCE_RELEASED' then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;
    perform 1 from public.contacts contact_row where contact_row.id = v_contact_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_engine_event_contact_not_found';
    end if;

    -- El flujo en su subtransaccion, como cada flujo de un evento de Hotmart.
    begin
        select one.* into v_one
          from public._flow_engine_admit_native_flow(
              v_control, v_event, v_contact_id, null::text, v_moment, v_account_id, v_inbox_id,
              v_destination) one;
    exception when others then
        get stacked diagnostics v_sqlstate = returned_sqlstate;
        perform public._flow_engine_record_start_rejection(
            v_control.flow_id, v_control.active_version, v_event.id, 'admision',
            'error_interno:' || lower(v_sqlstate));
        select 'motor_no_arranca'::text as outcome, 'error_interno:' || lower(v_sqlstate) as reason,
               null::uuid as recovery_case_id, null::uuid as followup_sequence_id,
               null::uuid as scheduled_action_id, null::boolean as created
          into v_one;
    end;
    return query select v_control.flow_id, v_one.outcome, v_one.reason, v_one.recovery_case_id,
        v_one.followup_sequence_id, v_one.scheduled_action_id, v_one.created;
end;
$function$;

-- 6b. _flow_engine_admit_native_flow: el permiso, el caso, el context y la autorizacion.
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'    elsif public._flow_engine_native_trigger(v_definition.definition) is distinct from ''compra''\n'
    || E'       or p_event.event_type is distinct from ''PURCHASE_APPROVED'' then\n',
    E'    -- e483_native_audience_permission: begin (20261112210600)\n'
    || E'    -- El disparo por audiencia: el permiso de la definicion, la conversacion\n'
    || E'    -- derivada o viva y la compra (_flow_engine_native_audience_reason).\n'
    || E'    elsif public._flow_engine_native_trigger(v_definition.definition) = ''audiencia''\n'
    || E'       and p_event.source = ''system'' and p_event.event_type = ''FLOW_AUDIENCE_RELEASED'' then\n'
    || E'        v_reason := public._flow_engine_native_audience_reason(\n'
    || E'            v_definition.definition, p_control.tenant_ref, p_control.product_ref, p_contact_id, p_destination);\n'
    || E'    -- e483_native_audience_permission: end\n'
    || E'    elsif public._flow_engine_native_trigger(v_definition.definition) is distinct from ''compra''\n'
    || E'       or p_event.event_type is distinct from ''PURCHASE_APPROVED'' then\n',
    'e483_native_audience_permission');
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'        case when public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
    || E'             then ''landing'' else p_event.source end,\n',
    E'        -- e483_native_audience_source: y la audiencia, que nace de un evento system (20261112210600).\n'
    || E'        case when public._flow_engine_native_trigger(v_definition.definition) in (''intencion'', ''audiencia'')\n'
    || E'             then ''landing'' else p_event.source end,\n',
    'e483_native_audience_source');
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'                else ''{}''::jsonb end,\n'
    || E'        case when public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
    || E'             then v_audience_intent end)\n',
    E'                else ''{}''::jsonb end\n'
    || E'        -- e483_native_audience_context: la ocurrencia de la lista (20261112210600).\n'
    || E'        || case when public._flow_engine_native_trigger(v_definition.definition) = ''audiencia''\n'
    || E'                then jsonb_build_object(''flow_audience_occurrence_id'', p_event.payload ->> ''ocurrencia_id'',\n'
    || E'                                        ''flow_audience_form'', p_event.payload ->> ''tipo'')\n'
    || E'                else ''{}''::jsonb end,\n'
    || E'        case when public._flow_engine_native_trigger(v_definition.definition) = ''intencion''\n'
    || E'             then v_audience_intent end)\n',
    'e483_native_audience_context');
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'            case when v_audience_intent is null then ''hotmart'' else ''system'' end,\n',
    E'            -- e483_native_audience_grant_source: la audiencia concede el sistema (20261112210600).\n'
    || E'            case when v_audience_intent is null\n'
    || E'                      and public._flow_engine_native_trigger(v_definition.definition) is distinct from ''audiencia''\n'
    || E'                 then ''hotmart'' else ''system'' end,\n',
    'e483_native_audience_grant_source');
select pg_temp.e483_native_audience_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    E'                 when v_audience_intent is null\n'
    || E'                 then jsonb_build_object(''reason'', ''purchase_approved''',
    E'                 -- e483_native_audience_grant: la lista de la ocurrencia (20261112210600).\n'
    || E'                 when public._flow_engine_native_trigger(v_definition.definition) = ''audiencia''\n'
    || E'                 then jsonb_build_object(''reason'', ''flow_audience'', ''webhook_event_id'', p_event.id,\n'
    || E'                                         ''flow_audience_occurrence_id'', p_event.payload ->> ''ocurrencia_id'',\n'
    || E'                                         ''permiso'', v_definition.definition #>> ''{entra,permiso}'',\n'
    || E'                                         ''recovery_case_id'', v_case_id)\n'
    || E'                 when v_audience_intent is null\n'
    || E'                 then jsonb_build_object(''reason'', ''purchase_approved''',
    'e483_native_audience_grant:');

-- ===========================================================================
-- 8. La compra como salida, en la reevaluacion.
-- ===========================================================================
create function public._flow_engine_native_audience_purchase_stop(p_case_id uuid)
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
    -- e483_native_audience_purchase_stop (20261112210600)
    select rc.* into v_case from public.recovery_cases rc where rc.id = p_case_id;
    select d.definition, c.tenant_ref, c.product_ref into v_definition, v_tenant, v_product
      from public.flow_engine_runs run
      join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id
      join public.flow_definitions d on d.flow_id = run.flow_id and d.version = run.version
      join public.flow_controls c on c.flow_id = run.flow_id
     where run.recovery_case_id = p_case_id and native.trigger_event = 'audiencia';
    if v_case.id is null or v_definition is null
       or not coalesce(jsonb_typeof(v_definition #> '{termina,si}') = 'array'
                       and v_definition #> '{termina,si}' @> '["compra"]'::jsonb, false) then
        return null;
    end if;
    select identity_row.external_user_id into v_destination
      from public.channel_identities identity_row
     where identity_row.id = v_case.selected_channel_identity_id and identity_row.contact_id = v_case.contact_id;
    -- Con permiso compra (clientas) solo cuenta una compra posterior al caso.
    if public._flow_engine_native_audience_purchased(
           v_tenant, v_product, v_case.contact_id, v_destination,
           case when v_definition #>> '{entra,permiso}' = 'compra' then v_case.created_at end) then
        return 'purchase_detected_any_offer';
    end if;
    return null;
end;
$function$;

select pg_temp.e483_native_audience_patch(
    'public._portable_case_purchase_stop_reason(uuid)',
    E'    if not found then\n        return null;\n    end if;\n\n    select binding.* into v_binding\n',
    E'    if not found then\n        return null;\n    end if;\n\n'
    || E'    -- e483_native_audience_case: un caso de audiencia mira la compra por su flujo, no\n'
    || E'    -- por el binding de su evento (es un evento system) (20261112210600).\n'
    || E'    if v_case.context ->> ''flow_engine_trigger'' = ''audiencia'' then\n'
    || E'        return public._flow_engine_native_audience_purchase_stop(p_case_id);\n'
    || E'    end if;\n\n'
    || E'    select binding.* into v_binding\n',
    'e483_native_audience_case');

-- ===========================================================================
-- 1. La entrada: solo con la clave del bridge y un evento de audiencia.
-- ===========================================================================
select pg_temp.e483_native_audience_patch(
    'public.admit_flow_engine_event_v1(jsonb,jsonb)',
    E'    v_instance := public._flow_store_instance(p_instance);\n',
    E'    v_instance := public._flow_store_instance(p_instance);\n\n'
    || E'    -- e483_native_audience_admit: begin (20261112210600)\n'
    || E'    -- La persona de la lista de un disparo por audiencia, atada a su flujo y\n'
    || E'    -- version, solo si el bridge lo pide (FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED).\n'
    || E'    if p_instance -> ''native_audience'' = ''true''::jsonb\n'
    || E'       and jsonb_typeof(p_flow_event) = ''object''\n'
    || E'       and p_flow_event -> ''fuente'' = ''"audiencia"''::jsonb then\n'
    || E'        return query select audience.* from public._flow_engine_admit_native_audience_v1(\n'
    || E'            p_instance, p_flow_event) audience;\n'
    || E'        return;\n'
    || E'    end if;\n'
    || E'    -- e483_native_audience_admit: end\n',
    'e483_native_audience_admit');

-- ===========================================================================
-- Los permisos: las funciones nuevas no son de la API.
-- ===========================================================================
do $acl$
declare
    v_signature text;
    v_role text;
begin
    foreach v_signature in array array[
        'public._flow_engine_admit_native_audience_v1(jsonb,jsonb)',
        'public._flow_engine_native_audience_reason(jsonb,text,text,uuid,text)',
        'public._flow_engine_native_audience_purchased(text,text,uuid,text,timestamptz)',
        'public._flow_engine_native_audience_purchase_stop(uuid)'
    ] loop
        execute format('revoke all on function %s from public', v_signature);
        foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
            if exists (select 1 from pg_roles where rolname = v_role) then
                execute format('revoke all on function %s from %I', v_signature, v_role);
                if has_function_privilege(v_role, v_signature, 'EXECUTE') then
                    raise exception using errcode = '55000', message = 'e483_native_audience_acl_unexpected',
                        detail = v_signature || ' ' || v_role;
                end if;
            end if;
        end loop;
    end loop;
end;
$acl$;

do $check$
begin
    if public._flow_engine_native_trigger('{"dispara": {"fecha": {"el": "2026-12-01T10:00", "zona": "America/Mexico_City"}}}'::jsonb) is distinct from 'audiencia'
       or public._flow_engine_native_trigger('{"dispara": {"manual": {}}}'::jsonb) is distinct from 'audiencia'
       or public._flow_engine_native_trigger('{"dispara": {"manual": {}, "fecha": {}}}'::jsonb) is not null
       or public._flow_engine_native_trigger('{"dispara": {"evento": "compra"}}'::jsonb) is distinct from 'compra'
       or jsonb_array_length(public._flow_engine_native_steps('nativo:audiencia')) <> 3
       or position('e483_native_audience_admit: end' in pg_get_functiondef(
              'public.admit_flow_engine_event_v1(jsonb,jsonb)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_native_audience_not_applied';
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
        check (trigger_event in ('compra', 'carrito', 'pago_fallido', 'intencion', 'audiencia'));

commit;
