-- flow-engine-native-silence-v1: la base del seguimiento a 24 h con cupon
-- nativo, el silencio en el que lo ultimo lo dijo el agente (E-251, P22-2 de
-- M16; diseno: documentacion/2026-11-01-m16-diseno-seguimiento-y-ficha-nativos.md,
-- 1.2 (b)). Va apilada sobre P22-1 (el esquema y el validador, #385).
--
-- Por que. El Automation Expert solo crea flujos nativos, y el seguimiento con
-- cupon de hoy es del legado (el barrido de followup_discount.py, con
-- CONVERSATION_FOLLOWUP_ENABLED): Maribel y ATT1 lo mandan con
-- claim_portable_conversation_followup_v1. P22-1 ya deja escribir el
-- seguimiento como un flujo nativo (dispara.silencio con lo_ultimo: agente,
-- entra.contesto_a: cualquier_plantilla), y la base lo rechazaba al publicar
-- (motor_nativo:disparador). Esta migracion lo deja publicar y le da su
-- reserva. El barrido nativo que la llama es P22-3: hasta entonces nadie la
-- llama.
--
-- Decisiones (con su default, D-P22-1 a D-P22-4 del diseno):
-- D-P22-1. La corrida nativa es la fila de conversation_followup_events con
--    flow_id, sin caso de recuperacion ni evento sintetico (webhook_events y
--    recovery_cases no aceptan source = flow_engine, y un caso con
--    conversation_id deja *_ambiguous_case en la conversacion).
-- D-P22-2. contesto_a: cualquier_plantilla es la adopcion del legado (el
--    evento de adopcion de la conversacion, de cualquier plantilla), que la
--    reserva portable ya mira (blocked_not_template_reply).
-- D-P22-3. Un solo envio, en el momento del barrido, sin espera.
-- D-P22-4. La barrera del legado es por instancia: los flujos del tenant y la
--    aliada del binding activo de la cuenta y el inbox de Chatwoot del claim,
--    y de ese producto (binding.funnel_ref = flow_controls.product_ref): con dos
--    productos en una instancia, cada silencio le saca al legado solo las
--    conversaciones del inbox de su binding.
--
-- Que cambia, por bloques:
--
-- 1. conversation_followup_events suma flow_id, flow_version y
--    definition_sha256, nulos para el legado (todas o ninguna). Sin clave
--    foranea al almacen, como flow_engine_runs con el sustrato: la unica que
--    escribe flow_id es la reserva nativa, que lo saca de flow_definitions.
--    Un indice parcial por flow_id para las lecturas.
-- 2. _flow_engine_native_trigger: un reemplazo contado, que suma
--    silencio_agente (dispara = {silencio: {lo_ultimo: agente, ...}}). Es un
--    reemplazo y no la funcion entera, para no pisar los disparadores que
--    sumen otras migraciones (la del primer contacto nativo, 20261101000100,
--    reemplaza esta funcion entera y suma intencion).
-- 3. _flow_engine_native_unsupported, entera (la vigente es la de
--    20261028000200): con el silencio, el permiso conversacion, contesto_a
--    cualquier_plantilla, una vez por conversacion y nunca otra, y un solo
--    paso: la plantilla con las variables [nombre, producto], el boton del
--    checkout con un cupon fijo y las salidas compra y contesta. Con los
--    eventos, igual que hoy.
-- 4. claim_flow_engine_silence_v1 (nueva): la reserva nativa, derivada del
--    cuerpo VIGENTE de claim_portable_conversation_followup_v1 antes de la
--    barrera del bloque 5 (pg_get_functiondef con reemplazos contados, el
--    metodo de 20261010000100). Suma, en este orden: el replay solo de una
--    corrida del mismo flujo; el control (prendido, o cohorte con el contacto
--    de la conversacion adentro), la ruta nativa, el disparador, la instancia,
--    la version activa con su sha256 y la plantilla, el idioma y el cupon del
--    paso fijado; la bolsa (su lock y los topes, contados por los dos caminos)
--    justo antes de emitir el link; y al final la fila con flow_id y su
--    autorizacion en flow_engine_request_authorizations. Todo lo demas es la
--    portable: el uno por conversacion, la adopcion, la derivacion, la
--    conversacion, el contacto, la compra y la emision con
--    reserve_portable_checkout_issuance_v2 anclada en el ultimo saliente. Un
--    rechazo no escribe nada: ni fila, ni link, ni bolsa.
-- 5. claim_portable_conversation_followup_v1, dos reemplazos contados:
--    * el replay de un command_key que reservo un flujo nativo no se le
--      devuelve al legado (blocked_followup_limit): si no, los dos barridos
--      mandarian el mismo mensaje;
--    * blocked_native_flow, despues de la adopcion: un silencio nativo del
--      agente de la instancia, prendido o en cohorte con ese contacto, le saca
--      la conversacion al legado.
--    Sin un silencio nativo, las dos ramas dan falso y la reserva da lo de hoy.
-- 6. read_flow_engine_native_trigger_counts_v1: la simulacion de un silencio
--    (p_trigger = silencio) cuenta los seguimientos sent de la ventana en el
--    inbox de los bindings del producto, y a cuantas personas: lo que habria
--    disparado, con la vara del legado. Un bloque al principio, que no toca
--    las ramas de los eventos.
-- 7. read_flow_sales_v1: un flujo con corridas del silencio entra a la
--    lectura, y la venta de una corrida se lee por su link
--    (flow_id -> checkout_issuance_id -> purchase_matched, by_link), antes de
--    la de identidad. Sin corridas del silencio, la lectura da lo de hoy.
-- 8. Los permisos: la reserva nativa solo la ejecuta service_role.
--
-- Lo que no cambia. claim_conversation_followup_v1 (la compartida, la de
-- Johanna) no se toca. settle_conversation_followup_v1 sirve para los dos
-- caminos (por command_key). La admision de eventos no ve el silencio (elige
-- los flujos por dispara.evento). El indice de uno por conversacion
-- (20260928000400) es el que cruza los dos caminos: legado y nativo nunca le
-- mandan dos veces a la misma conversacion. El resultado nuevo del legado
-- (blocked_native_flow) y los de la reserva nativa estan en
-- FOLLOWUP_CLAIM_OUTCOMES del bridge en el mismo PR.
--
-- Lo que no hace: no crea roles, no cambia el ACL de lo que ya existe y no
-- toca filas. Correrla dos veces aborta con 55000 sin cambiar nada. El bloque
-- 1 toma ACCESS EXCLUSIVE sobre conversation_followup_events (tabla viva y
-- chica: decenas de filas): va con lock_timeout.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)') is null
       or to_regprocedure('public._flow_engine_native_unsupported(jsonb)') is null
       or to_regprocedure('public.read_flow_sales_v1(jsonb,timestamptz)') is null
       or to_regprocedure('public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)') is null
       or to_regclass('public.flow_engine_pools') is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_missing';
    end if;
    if to_regprocedure('public.claim_flow_engine_silence_v1(uuid,integer,text,bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)') is not null
       or exists (select 1 from pg_attribute a
                   where a.attrelid = 'public.conversation_followup_events'::regclass
                     and a.attname = 'flow_id' and not a.attisdropped) then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla, con la misma verificacion para todos.
create function pg_temp.flow_engine_native_silence_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_silence_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_definition_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- Bloque 1. La corrida: la fila del seguimiento con su flujo.
-- ===========================================================================

alter table public.conversation_followup_events
    add column flow_id uuid,
    add column flow_version integer,
    add column definition_sha256 text,
    add constraint conversation_followup_events_native_run check (
        (flow_id is null and flow_version is null and definition_sha256 is null)
        or (flow_id is not null and flow_version is not null and flow_version > 0
            and definition_sha256 is not null and definition_sha256 ~ '^[0-9a-f]{64}$'));

create index conversation_followup_events_flow_idx
    on public.conversation_followup_events (flow_id, created_at)
    where flow_id is not null;

-- ===========================================================================
-- Bloque 2. El disparador.
-- ===========================================================================

-- El freno: el disparador vigente tiene que ser el de los eventos que conocemos
-- (compra, carrito y pago rechazado), con un solo select case. Si otra migracion
-- lo cambio de forma, aborta en vez de parchear a ciegas.
do $trigger_guard$
declare
    v_definition text := pg_get_functiondef('public._flow_engine_native_trigger(jsonb)'::regprocedure);
begin
    if length(v_definition) - length(replace(v_definition, E'    select case\n', '')) <> length(E'    select case\n')
       or position('''{"evento": "compra"}''::jsonb then ''compra''' in v_definition) = 0
       or position('''{"evento": "carrito"}''::jsonb then ''carrito''' in v_definition) = 0
       or position('''{"evento": "pago_fallido"}''::jsonb then ''pago_fallido''' in v_definition) = 0
       or position('flow_engine_native_silence_trigger' in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_trigger_definition';
    end if;
end;
$trigger_guard$;

select pg_temp.flow_engine_native_silence_patch(
    'public._flow_engine_native_trigger(jsonb)',
    E'    select case\n',
    E'    select case\n'
    || E'        -- flow_engine_native_silence_trigger: el silencio en que lo ultimo lo dijo el\n'
    || E'        -- agente, el seguimiento con cupon (20261101000200). Solo la clave silencio.\n'
    || E'        when jsonb_typeof(p_definition -> ''dispara'') = ''object''\n'
    || E'             and (p_definition -> ''dispara'') - ''silencio'' = ''{}''::jsonb\n'
    || E'             and jsonb_typeof(p_definition #> ''{dispara,silencio}'') = ''object''\n'
    || E'             and p_definition #> ''{dispara,silencio,lo_ultimo}'' = ''"agente"''::jsonb then ''silencio_agente''\n',
    'flow_engine_native_silence_trigger');

-- ===========================================================================
-- Bloque 3. Lo que el motor nativo ejecuta.
-- ===========================================================================

-- Por que el motor nativo de esta version no ejecuta una definicion, o null.
-- Con los eventos, la de 20261028000200 tal cual: la compra con el permiso
-- compra, los demas con formulario, por persona o por persona y oferta, y un
-- solo envio. Con el silencio del agente (20261101000200), lo que hace el
-- barrido del legado: permiso conversacion, contesto_a cualquier_plantilla, una
-- vez por conversacion y nunca otra, y un solo paso, la plantilla con
-- [nombre, producto], el boton del checkout con un cupon fijo (la forma que
-- acepta la reserva, no el personal) y las salidas compra y contesta.
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
        -- flow_engine_native_silence: el silencio entra con conversacion (20261101000200).
        when p_definition #> '{entra,permiso}' is distinct from
             (case public._flow_engine_native_trigger(p_definition)
                  when 'compra' then '"compra"'
                  when 'silencio_agente' then '"conversacion"'
                  else '"formulario"' end)::jsonb then 'motor_nativo:permiso'
        -- flow_engine_native_silence_entry: la conversacion adoptada, de cualquier plantilla.
        when public._flow_engine_native_trigger(p_definition) = 'silencio_agente'
             and p_definition #> '{entra,contesto_a}' is distinct from '"cualquier_plantilla"'::jsonb
            then 'motor_nativo:permiso'
        -- flow_engine_native_silence_repeat: una vez por conversacion, y nunca otra
        -- (el indice unico de conversation_followup_events, 20260928000400).
        when public._flow_engine_native_trigger(p_definition) = 'silencio_agente'
             and (p_definition #> '{topes,repeticion,una_por}' is distinct from '"conversacion"'::jsonb
                  or p_definition #> '{topes,repeticion,otra_despues_de}' is distinct from '"nunca"'::jsonb)
            then 'motor_nativo:repeticion'
        when public._flow_engine_native_trigger(p_definition) is distinct from 'silencio_agente'
             and coalesce(p_definition #>> '{topes,repeticion,una_por}', '') not in ('persona', 'persona_y_oferta')
            then 'motor_nativo:repeticion'
        when public._flow_engine_native_trigger(p_definition) is distinct from 'silencio_agente'
             and p_definition #> '{topes,repeticion,otra_despues_de}' is distinct from '"nunca"'::jsonb
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
        -- flow_engine_native_silence_shape: el silencio es el envio del barrido, en el
        -- momento: un solo paso, la plantilla, sin espera; las variables del barrido en
        -- su orden; el boton del checkout con un cupon fijo que la reserva acepta; y la
        -- compra y la respuesta cortan (20261101000200).
        when public._flow_engine_native_trigger(p_definition) = 'silencio_agente'
             and not coalesce(
                 jsonb_typeof(p_definition -> 'pasos') = 'array'
                 and jsonb_array_length(p_definition -> 'pasos') = 1
                 and jsonb_typeof(p_definition #> '{pasos,0,plantilla}') = 'string'
                 and p_definition #> '{pasos,0,variables}' = '["nombre", "producto"]'::jsonb
                 and p_definition #>> '{pasos,0,boton,link}' = 'checkout'
                 and jsonb_typeof(p_definition #> '{pasos,0,boton,cupon}') = 'string'
                 and p_definition #>> '{pasos,0,boton,cupon}' ~ '^[A-Za-z0-9_-]{1,64}$'
                 and p_definition #>> '{pasos,0,boton,cupon}' <> 'personal'
                 and jsonb_typeof(p_definition #> '{termina,si}') = 'array'
                 and p_definition #> '{termina,si}' @> '["compra", "contesta"]'::jsonb, false)
            then 'motor_nativo:forma'
    end
$function$;

-- ===========================================================================
-- Bloque 4. La reserva nativa, derivada de la portable antes de su barrera.
-- ===========================================================================

do $silence_claim$
declare
    v_portable constant text :=
        'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)';
    v_native constant text :=
        'public.claim_flow_engine_silence_v1(uuid,integer,text,bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)';
    -- Los textos de la portable que se reemplazan, cada uno exactamente una vez.
    c_head constant text := 'public.claim_portable_conversation_followup_v1(';
    c_declare constant text := E'    v_now timestamptz;\nbegin\n';
    c_now constant text := E'    v_now := coalesce(p_now, clock_timestamp());\n';
    c_replay constant text := E'    if v_existing.id is not null then\n';
    c_limit constant text := E'    -- Uno solo por conversacion, venga del command_key que venga.\n';
    c_reserve constant text :=
        E'    -- El link: la misma emision que usa el agente, anclada en nuestro ultimo\n'
        || E'    -- mensaje. La RPC repite sus propias barreras (caso, scope, contacto,\n'
        || E'    -- opt-out, conversacion, identidad, compra) y resuelve la oferta del lead.\n'
        || E'    select reservation.* into v_reservation\n';
    c_columns constant text := E'        inbound_age_seconds, checkout_issuance_id, created_at\n    ) values (\n';
    c_values constant text :=
        E'        p_inbound_age_seconds, v_reservation.issuance_id, v_now\n    )\n    returning id into v_event_id;\n';
    v_anchors text[];
    v_replacements text[];
    v_definition text;
    v_index integer;
begin
    if (select p.proowner from pg_proc p where p.oid = v_portable::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_owner_mismatch',
            detail = v_portable;
    end if;
    v_definition := pg_get_functiondef(v_portable::regprocedure);
    -- La portable de 20261010000100 y nada encima: con la reserva portable y la
    -- barrera de la adopcion, sin la barrera del nativo.
    if position('reserve_portable_checkout_issuance_v2' in v_definition) = 0
       or position('portable_followup_template_reply: end' in v_definition) = 0
       or position('blocked_native_flow' in v_definition) > 0
       or position('flow_engine_native_silence' in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_conversation_followup_claim_definition',
            detail = v_portable;
    end if;

    v_anchors := array[c_head, c_declare, c_now, c_replay, c_limit, c_reserve, c_columns, c_values];
    v_replacements := array[
        -- El nombre y los tres argumentos del flujo, delante de los de la portable.
        'public.claim_flow_engine_silence_v1(p_flow_id uuid, p_version integer, p_definition_sha256 text, ',

        E'    v_now timestamptz;\n'
        || E'    -- flow_engine_native_silence_claim: lo que suma la reserva nativa (20261101000200).\n'
        || E'    v_control public.flow_controls%rowtype;\n'
        || E'    v_definition public.flow_definitions%rowtype;\n'
        || E'    v_pool public.flow_engine_pools%rowtype;\n'
        || E'    v_local_date date;\n'
        || E'    v_total integer;\n'
        || E'    v_daily integer;\n'
        || E'begin\n',

        E'    v_now := coalesce(p_now, clock_timestamp());\n'
        || E'    -- flow_engine_native_silence_input: el flujo, la version y su sha256.\n'
        || E'    if p_flow_id is null or p_version is null or p_version <= 0\n'
        || E'       or p_definition_sha256 is null or p_definition_sha256 !~ ''^[0-9a-f]{64}$'' then\n'
        || E'        raise exception using errcode = ''22023'',\n'
        || E'            message = ''claim_flow_engine_silence_invalid_input'';\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_silence_trigger_guard: si otra migracion reemplazo entero el\n'
        || E'    -- disparador sin el silencio (el reemplazo contado de 20261101000200), la reserva\n'
        || E'    -- falla fuerte en vez de dar blocked_flow_not_running para siempre.\n'
        || E'    if position(''flow_engine_native_silence_trigger'' in\n'
        || E'                pg_get_functiondef(''public._flow_engine_native_trigger(jsonb)''::regprocedure)) = 0 then\n'
        || E'        raise exception using errcode = ''55000'',\n'
        || E'            message = ''flow_engine_native_silence_trigger_missing'';\n'
        || E'    end if;\n',

        E'    -- flow_engine_native_silence_replay: el replay es solo de una corrida de este\n'
        || E'    -- flujo. La del legado o la de otro flujo con el mismo command_key ya es de\n'
        || E'    -- otro barrido: devolverla haria que los dos manden el mismo mensaje.\n'
        || E'    if v_existing.id is not null and v_existing.flow_id is distinct from p_flow_id then\n'
        || E'        outcome := ''blocked_followup_limit'';\n'
        || E'        return next;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    if v_existing.id is not null then\n',

        E'    -- flow_engine_native_silence_gate: begin\n'
        || E'    -- El flujo (20261101000200): un silencio nativo del agente, de la instancia\n'
        || E'    -- del claim (el binding activo de la cuenta y el inbox), prendido o en\n'
        || E'    -- cohorte, con la version que ejecuta el barrido activa y el mismo sha256,\n'
        || E'    -- y la plantilla, el idioma y el cupon de su paso. Nada de esto escribe.\n'
        || E'    select c.* into v_control from public.flow_controls c where c.flow_id = p_flow_id for share;\n'
        || E'    select d.* into v_definition from public.flow_definitions d\n'
        || E'     where d.flow_id = p_flow_id and d.version = p_version;\n'
        || E'    if v_control.flow_id is null or v_definition.flow_id is null\n'
        || E'       or v_control.state not in (''prendido'', ''cohorte'')\n'
        || E'       or not exists (select 1 from public.flow_engine_routes native_route\n'
        || E'                       where native_route.flow_id = p_flow_id and native_route.legacy_kind is null)\n'
        || E'       or public._flow_engine_native_trigger(v_definition.definition) is distinct from ''silencio_agente''\n'
        || E'       or not exists (select 1 from public.commercial_ally_runtime_bindings binding\n'
        || E'                       where binding.tenant_ref = v_control.tenant_ref\n'
        || E'                         and binding.ally_ref = v_control.ally_ref\n'
        || E'                         and binding.funnel_ref = v_control.product_ref\n'
        || E'                         and binding.status = ''active''\n'
        || E'                         and binding.chatwoot_account_id = p_chatwoot_account_id\n'
        || E'                         and binding.chatwoot_inbox_id = p_chatwoot_inbox_id) then\n'
        || E'        outcome := ''blocked_flow_not_running'';\n'
        || E'        return next;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    if v_control.active_version is distinct from p_version\n'
        || E'       or v_definition.definition_sha256 is distinct from p_definition_sha256\n'
        || E'       or public._flow_engine_native_unsupported(v_definition.definition) is not null\n'
        || E'       or v_definition.definition #>> ''{pasos,0,plantilla}'' is distinct from btrim(p_template_name)\n'
        || E'       or (v_definition.definition #> ''{pasos,0}'' ? ''idioma''\n'
        || E'           and v_definition.definition #>> ''{pasos,0,idioma}'' is distinct from btrim(p_template_language))\n'
        || E'       or v_definition.definition #>> ''{pasos,0,boton,cupon}'' is distinct from p_coupon_code then\n'
        || E'        outcome := ''blocked_flow_version'';\n'
        || E'        return next;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    if v_control.state = ''cohorte'' and not exists (\n'
        || E'        select 1 from public.flow_cohort_members member\n'
        || E'         where member.flow_id = p_flow_id and member.contact_id = v_conversation.contact_id) then\n'
        || E'        outcome := ''blocked_out_of_cohort'';\n'
        || E'        return next;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_silence_gate: end\n'
        || E'\n'
        || E'    -- Uno solo por conversacion, venga del command_key que venga.\n',

        E'    -- flow_engine_native_silence_pool: begin\n'
        || E'    -- La bolsa (contrato de flow-engine-v2, seccion 3): su lock y los topes de la\n'
        || E'    -- version fijada, contados por los dos caminos, con el dia en su zona. Va\n'
        || E'    -- despues de todas las barreras y antes de emitir el link: un tope no deja\n'
        || E'    -- un link reservado ni gasta nada.\n'
        || E'    select pool.* into v_pool from public.flow_engine_pools pool\n'
        || E'     where pool.pool = public._flow_engine_native_pool(v_definition.definition) for update;\n'
        || E'    if v_pool.pool is null then\n'
        || E'        outcome := ''blocked_flow_cap'';\n'
        || E'        return next;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    v_local_date := (v_now at time zone v_pool.timezone)::date;\n'
        || E'    select (select count(*)::integer from public.pilot_outbound_request_authorizations authrow\n'
        || E'             where authrow.scope_key = v_pool.pool)\n'
        || E'           + (select count(*)::integer from public.flow_engine_request_authorizations engine_row\n'
        || E'               where engine_row.pool = v_pool.pool)\n'
        || E'      into v_total;\n'
        || E'    select (select count(*)::integer from public.pilot_outbound_request_authorizations authrow\n'
        || E'             where authrow.scope_key = v_pool.pool and authrow.local_budget_date = v_local_date)\n'
        || E'           + (select count(*)::integer from public.flow_engine_request_authorizations engine_row\n'
        || E'               where engine_row.pool = v_pool.pool and engine_row.local_budget_date = v_local_date)\n'
        || E'      into v_daily;\n'
        || E'    if (jsonb_typeof(v_definition.definition #> ''{topes,total}'') = ''number''\n'
        || E'        and v_total >= (v_definition.definition #>> ''{topes,total}'')::numeric)\n'
        || E'       or (jsonb_typeof(v_definition.definition #> ''{topes,por_dia}'') = ''number''\n'
        || E'           and v_daily >= (v_definition.definition #>> ''{topes,por_dia}'')::numeric) then\n'
        || E'        outcome := ''blocked_flow_cap'';\n'
        || E'        return next;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_silence_pool: end\n'
        || E'\n'
        || E'    -- El link: la misma emision que usa el agente, anclada en nuestro ultimo\n'
        || E'    -- mensaje. La RPC repite sus propias barreras (caso, scope, contacto,\n'
        || E'    -- opt-out, conversacion, identidad, compra) y resuelve la oferta del lead.\n'
        || E'    select reservation.* into v_reservation\n',

        E'        inbound_age_seconds, checkout_issuance_id, created_at,\n'
        || E'        -- flow_engine_native_silence_run: la corrida es esta fila (D-P22-1).\n'
        || E'        flow_id, flow_version, definition_sha256\n'
        || E'    ) values (\n',

        E'        p_inbound_age_seconds, v_reservation.issuance_id, v_now,\n'
        || E'        p_flow_id, p_version, v_definition.definition_sha256\n'
        || E'    )\n'
        || E'    returning id into v_event_id;\n'
        || E'\n'
        || E'    -- flow_engine_native_silence_authorization: la bolsa la gasta solo una reserva.\n'
        || E'    -- El seguimiento no tiene accion ni intento: la fila de la corrida es los dos.\n'
        || E'    insert into public.flow_engine_request_authorizations (\n'
        || E'        flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id,\n'
        || E'        local_budget_date, flow_generation, authorized_at)\n'
        || E'    values (\n'
        || E'        p_flow_id, p_version, v_pool.pool, null, v_event_id, v_event_id, v_conversation.contact_id,\n'
        || E'        v_local_date, v_control.generation, v_now);\n'
    ];

    for v_index in 1 .. array_length(v_anchors, 1) loop
        if length(v_definition) - length(replace(v_definition, v_anchors[v_index], ''))
           <> length(v_anchors[v_index]) then
            raise exception using errcode = '55000', message = 'unexpected_conversation_followup_claim_definition',
                detail = v_portable || ' ancla ' || v_index::text;
        end if;
    end loop;
    for v_index in 1 .. array_length(v_anchors, 1) loop
        v_definition := replace(v_definition, v_anchors[v_index], v_replacements[v_index]);
    end loop;
    execute v_definition;

    if to_regprocedure(v_native) is null
       or position('flow_engine_native_silence_gate: end' in pg_get_functiondef(v_native::regprocedure)) = 0
       or position('flow_engine_native_silence_authorization' in pg_get_functiondef(v_native::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_claim_not_created',
            detail = v_native;
    end if;
end;
$silence_claim$;

-- ===========================================================================
-- Bloque 5. La barrera del legado.
-- ===========================================================================

-- El replay de una corrida nativa no es del legado.
select pg_temp.flow_engine_native_silence_patch(
    'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)',
    E'    if v_existing.id is not null then\n',
    E'    -- flow_engine_native_silence_legacy_replay: un command_key que reservo un flujo\n'
    || E'    -- nativo (20261101000200) no se le devuelve al legado: ya lo manda el barrido\n'
    || E'    -- nativo. Sin flujos nativos, flow_id es siempre nulo.\n'
    || E'    if v_existing.id is not null and v_existing.flow_id is not null then\n'
    || E'        outcome := ''blocked_followup_limit'';\n'
    || E'        return next;\n'
    || E'        return;\n'
    || E'    end if;\n'
    || E'    if v_existing.id is not null then\n',
    'flow_engine_native_silence_legacy_replay');

-- blocked_native_flow, despues de la adopcion.
select pg_temp.flow_engine_native_silence_patch(
    'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)',
    E'    -- portable_followup_template_reply: end\n',
    E'    -- portable_followup_template_reply: end\n'
    || E'\n'
    || E'    -- flow_engine_native_silence_barrier: begin\n'
    || E'    -- Un silencio nativo del agente de esta instancia (el binding activo de la\n'
    || E'    -- cuenta y el inbox: D-P22-4), prendido, o en cohorte con este contacto\n'
    || E'    -- adentro, le saca la conversacion al legado (20261101000200). Sin un\n'
    || E'    -- silencio nativo, no hay fila que lo cumpla.\n'
    || E'    if exists (\n'
    || E'        select 1\n'
    || E'          from public.flow_controls native_control\n'
    || E'          join public.flow_engine_routes native_route\n'
    || E'            on native_route.flow_id = native_control.flow_id and native_route.legacy_kind is null\n'
    || E'          join public.flow_definitions native_definition\n'
    || E'            on native_definition.flow_id = native_control.flow_id\n'
    || E'           and native_definition.version = native_control.active_version\n'
    || E'         where public._flow_engine_native_trigger(native_definition.definition) = ''silencio_agente''\n'
    || E'           and exists (select 1 from public.commercial_ally_runtime_bindings binding\n'
    || E'                        where binding.tenant_ref = native_control.tenant_ref\n'
    || E'                          and binding.ally_ref = native_control.ally_ref\n'
    || E'                          and binding.funnel_ref = native_control.product_ref\n'
    || E'                          and binding.status = ''active''\n'
    || E'                          and binding.chatwoot_account_id = p_chatwoot_account_id\n'
    || E'                          and binding.chatwoot_inbox_id = p_chatwoot_inbox_id)\n'
    || E'           and (native_control.state = ''prendido''\n'
    || E'                or (native_control.state = ''cohorte''\n'
    || E'                    and exists (select 1 from public.flow_cohort_members member\n'
    || E'                                 where member.flow_id = native_control.flow_id\n'
    || E'                                   and member.contact_id = v_conversation.contact_id)))\n'
    || E'    ) then\n'
    || E'        outcome := ''blocked_native_flow'';\n'
    || E'        return next;\n'
    || E'        return;\n'
    || E'    end if;\n'
    || E'    -- flow_engine_native_silence_barrier: end\n',
    'flow_engine_native_silence_barrier');

-- ===========================================================================
-- Bloque 6. La simulacion de un silencio.
-- ===========================================================================

select pg_temp.flow_engine_native_silence_patch(
    'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)',
    E'        raise exception using errcode = ''22023'', message = ''flow_store_product_not_bound'';\n'
    || E'    end if;\n',
    E'        raise exception using errcode = ''22023'', message = ''flow_store_product_not_bound'';\n'
    || E'    end if;\n'
    || E'    -- flow_engine_native_silence_counts: begin\n'
    || E'    -- El silencio (20261101000200): los seguimientos que salieron en la ventana\n'
    || E'    -- en el inbox de los bindings del producto, y a cuantas personas. Es lo que\n'
    || E'    -- habria disparado, medido con la vara del legado.\n'
    || E'    if p_trigger = ''silencio'' then\n'
    || E'        if p_since is null or p_since > v_now then\n'
    || E'            raise exception using errcode = ''22023'', message = ''flow_engine_history_since_invalid'';\n'
    || E'        end if;\n'
    || E'        if p_since < v_now - interval ''8 days'' then\n'
    || E'            raise exception using errcode = ''22023'', message = ''flow_engine_history_window_too_wide'';\n'
    || E'        end if;\n'
    || E'        select count(*)::integer, count(distinct issuance.contact_id)::integer\n'
    || E'          into v_events, v_people\n'
    || E'          from public.conversation_followup_events followup\n'
    || E'          join public.checkout_link_issuances issuance on issuance.id = followup.checkout_issuance_id\n'
    || E'         where followup.status = ''sent''\n'
    || E'           and followup.created_at >= p_since\n'
    || E'           and exists (select 1 from public.commercial_ally_runtime_bindings b\n'
    || E'                        where b.tenant_ref = v_instance.o_tenant_ref and b.ally_ref = v_instance.o_ally_ref\n'
    || E'                          and b.funnel_ref = p_product_ref\n'
    || E'                          and b.chatwoot_account_id = issuance.chatwoot_account_id\n'
    || E'                          and b.chatwoot_inbox_id = issuance.chatwoot_inbox_id);\n'
    || E'        return jsonb_build_object(''contract'', ''flow-engine-v2'', ''trigger'', p_trigger, ''product_ref'', p_product_ref,\n'
    || E'            ''since'', p_since, ''until'', v_now, ''events'', v_events, ''people'', v_people);\n'
    || E'    end if;\n'
    || E'    -- flow_engine_native_silence_counts: end\n',
    'flow_engine_native_silence_counts');

-- ===========================================================================
-- Bloque 7. Las ventas de un silencio, por su link.
-- ===========================================================================

do $sales$
declare
    c_sales constant text := 'public.read_flow_sales_v1(jsonb,timestamptz)';
begin
    -- Un flujo con corridas del silencio entra a la lectura.
    perform pg_temp.flow_engine_native_silence_patch(c_sales,
        E'           and exists (select 1 from public.flow_engine_runs run where run.flow_id = c.flow_id)\n',
        E'           and (exists (select 1 from public.flow_engine_runs run where run.flow_id = c.flow_id)\n'
        || E'                -- flow_engine_native_silence_sales_flows: o corridas del silencio (20261101000200).\n'
        || E'                or exists (select 1 from public.conversation_followup_events followup\n'
        || E'                            where followup.flow_id = c.flow_id))\n',
        'flow_engine_native_silence_sales_flows');

    -- La venta de una corrida del silencio, por su link, antes de la de identidad.
    perform pg_temp.flow_engine_native_silence_patch(c_sales,
        E'    by_identity as (\n',
        E'    -- flow_engine_native_silence_sales_link: begin\n'
        || E'    -- Las corridas del silencio (20261101000200): la fila del seguimiento con su\n'
        || E'    -- flujo, y la compra con su link (checkout_issuance_id -> purchase_matched).\n'
        || E'    -- Una compra atada a un caso cuenta por el caso.\n'
        || E'    silence_runs as (\n'
        || E'        select followup.id, followup.flow_id, followup.status, followup.created_at,\n'
        || E'               issuance.contact_id, issuance.status as issuance_status,\n'
        || E'               issuance.hotmart_webhook_event_id\n'
        || E'          from public.conversation_followup_events followup\n'
        || E'          join flows on flows.flow_id = followup.flow_id\n'
        || E'          join public.checkout_link_issuances issuance on issuance.id = followup.checkout_issuance_id\n'
        || E'    ),\n'
        || E'    by_link as (\n'
        || E'        select purchases.id, silence_runs.flow_id, silence_runs.contact_id as person_ref,\n'
        || E'               ''seguimiento''::text as step_key\n'
        || E'          from purchases\n'
        || E'          join silence_runs on silence_runs.hotmart_webhook_event_id = purchases.id\n'
        || E'                           and silence_runs.issuance_status = ''purchase_matched''\n'
        || E'                           and silence_runs.status = ''sent''\n'
        || E'         where not exists (select 1 from by_case where by_case.id = purchases.id)\n'
        || E'    ),\n'
        || E'    -- flow_engine_native_silence_sales_link: end\n'
        || E'    by_identity as (\n',
        'flow_engine_native_silence_sales_link');

    perform pg_temp.flow_engine_native_silence_patch(c_sales,
        E'         where not exists (select 1 from public.recovery_cases rc where rc.purchase_event_id = purchases.id)\n',
        E'         where not exists (select 1 from public.recovery_cases rc where rc.purchase_event_id = purchases.id)\n'
        || E'           -- flow_engine_native_silence_sales_once: la del link ya conto.\n'
        || E'           and not exists (select 1 from by_link where by_link.id = purchases.id)\n',
        'flow_engine_native_silence_sales_once');

    perform pg_temp.flow_engine_native_silence_patch(c_sales,
        E'        select by_identity.*, ''identity''::text from by_identity\n',
        E'        select by_identity.*, ''identity''::text from by_identity\n'
        || E'        -- flow_engine_native_silence_sales_union\n'
        || E'        union all\n'
        || E'        select by_link.*, ''link''::text from by_link\n',
        'flow_engine_native_silence_sales_union');

    -- Las corridas del silencio cuentan como recorridos, y by_link sale aparte.
    perform pg_temp.flow_engine_native_silence_patch(c_sales,
        E'                                   and runs.started_at >= p_since and runs.started_at < v_now),\n',
        E'                                   and runs.started_at >= p_since and runs.started_at < v_now)\n'
        || E'                             -- flow_engine_native_silence_sales_runs\n'
        || E'                             + (select count(*)::integer from silence_runs\n'
        || E'                                 where silence_runs.flow_id = flows.flow_id\n'
        || E'                                   and silence_runs.created_at >= p_since\n'
        || E'                                   and silence_runs.created_at < v_now),\n'
        || E'               ''by_link'', (select count(*)::integer from sales\n'
        || E'                            where sales.flow_id = flows.flow_id and sales.attached_by = ''link''),\n',
        'flow_engine_native_silence_sales_runs');
end;
$sales$;

-- ===========================================================================
-- Los permisos. La reserva nativa es un entrypoint del bridge: solo
-- service_role. Los privilegios por defecto de Supabase conceden directo a
-- cada rol, asi que se revoca a cada uno. Las demas conservan los suyos
-- (create or replace y los reemplazos no los cambian).
-- ===========================================================================

do $acl$
declare
    c_native constant regprocedure :=
        'public.claim_flow_engine_silence_v1(uuid,integer,text,bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)';
    v_role text;
begin
    execute format('revoke all on function %s from public', c_native);
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function %s from %I', c_native, v_role);
    end loop;
    if to_regrole('service_role') is not null then
        execute format('grant execute on function %s to service_role', c_native);
    end if;

    -- Por presencia: service_role la ejecuta y ningun rol de la API.
    if exists (select 1 from pg_roles where rolname = 'service_role'
                  and not has_function_privilege(rolname, c_native, 'EXECUTE'))
       or exists (select 1 from pg_roles where rolname in ('anon', 'authenticated')
                     and has_function_privilege(rolname, c_native, 'EXECUTE')) then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_acl_unexpected',
            detail = 'solo service_role ejecuta claim_flow_engine_silence_v1';
    end if;
end;
$acl$;

commit;
