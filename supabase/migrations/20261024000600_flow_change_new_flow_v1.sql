-- flow-change-new-flow-v1: el Automation Expert crea flujos (el PR 4 del hito 4).
-- Contratos: docs/contracts/flow-change-cycle-v1.md (1.7.0, «Crear un flujo: el
-- item nuevo y la cohorte») y docs/contracts/flow-engine-v2.md (secciones 6, 7
-- y 14). Diseno: «el hito 4: el motor general y el AE que crea flujos»
-- (2026-10-24, en el vault), pieza C. Decisiones D2 y D6 de Dan (2026-10-24).
--
-- Lo que trae, por bloques:
--
-- 1. Dos eventos nuevos del almacen: flow_created (la publicacion de un item
--    nuevo) y flow_cohort_changed (cohorte agregar o sacar del operador).
-- 2. El item nuevo en el ciclo, con reemplazos por ancla sobre la definicion
--    VIGENTE (el metodo de 20261024000200):
--    * _flow_change_items_are_valid: la quinta clase de item, sin flow_id, con
--      la forma de una version (la definicion como borrador y el OK del
--      cliente) y prender. A lo sumo uno por producto y flujo;
--    * _flow_change_item_flow_ids: un item nuevo no tiene flow_id y no entra
--      en la base ni en la generacion;
--    * _flow_change_save_draft: la base de un nuevo es «no hay un flujo con esa
--      clave en ese producto» (flow_already_registered), prender vale solo
--      cohorte (new_flow_must_start_in_cohort) y el Automation Expert puede
--      proponerlo (flow_creation_not_enabled sigue para una version sobre un
--      flujo registrado sin version: el camino del legado, que registra el
--      operador);
--    * _flow_change_publish: antes de escribir, lo que impide abrir el flujo
--      vuelve como resultado (_flow_change_new_flow_blocker); despues de pasar
--      la propuesta a publicada, en la misma transaccion: registra el control,
--      publica la v1, lo abre en cohorte (_flow_engine_open_native_flow, de
--      20261024000200: la bolsa, la ruta nativa y el control) y deja
--      flow_created. Si algo falla, no se publica nada;
--    * operator_publish_flow_change_v1: pasa la zona de la instancia
--      (p_instance.time_zone) a la apertura, para la bolsa de un flujo con
--      horario: siempre;
--    * read_flow_store_v1: cada flujo dice si es nativo (native).
-- 3. La cohorte del operador: operator_flow_cohort_v1 agrega o saca un
--    contacto, resuelto por el telefono que lee el comando de la terminal.
--    Hasta 5 por flujo, solo en un flujo nativo. No devuelve el telefono ni el
--    contacto.
-- 4. La simulacion de un flujo nuevo: read_flow_engine_native_trigger_counts_v1
--    cuenta las compras aprobadas del producto en la ventana de la vista
--    previa, y a cuantas personas. La ejecutan service_role y
--    automation_expert_change (la ruta de validar del agente), como las
--    lecturas del motor (20261021000200).
--
-- Lo que no hace: no crea flujos, bolsas ni rutas; no cambia filas; no toca el
-- carrito, el legado ni las lecturas de la v1. Sin un item nuevo, cada rama
-- nueva da falso y el ciclo hace lo de antes. No crea roles. Correrla dos
-- veces aborta con 55000 sin cambiar nada.
--
-- Las vigentes de lo que se reemplaza:
--   _flow_change_items_are_valid ......... 20261016000100
--   _flow_change_item_flow_ids ........... 20261016000100
--   _flow_change_save_draft .............. 20261016000100
--   _flow_change_publish ................. 20261016000100
--   operator_publish_flow_change_v1 ...... 20261016000100
--   read_flow_store_v1 ................... 20261021000100
--
-- Errores: 22023 entrada invalida; P0002 no existe; 55000 un estado que no lo
-- permite; 23514 un invariante.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_engine_open_native_flow(uuid,uuid,text)') is null
       or to_regprocedure('public.read_flow_engine_native_flows_v1(jsonb)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_missing';
    end if;
    if to_regprocedure('public.operator_flow_cohort_v1(jsonb,text,text,text,text,text,text)') is not null then
        raise exception using errcode = '55000', message = 'flow_change_new_flow_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla, con la misma verificacion para todos.
create function pg_temp.flow_change_new_flow_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
    v_acl_before text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_change_new_flow_function_missing', detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_change_new_flow_owner_mismatch', detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_change_new_flow_definition',
            detail = p_signature;
    end if;
    select coalesce(p.proacl::text, '') into v_acl_before from pg_proc p where p.oid = p_signature::regprocedure;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_change_new_flow_definition_not_replaced',
            detail = p_signature;
    end if;
    -- create or replace conserva el duenio y los permisos: se verifica.
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from v_acl_before then
        raise exception using errcode = '55000', message = 'flow_change_new_flow_acl_changed', detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- Bloque 1. Los eventos nuevos.
-- ===========================================================================

alter table public.flow_change_events
    drop constraint flow_change_events_event_type_check,
    add constraint flow_change_events_event_type_check check (event_type in (
        'flow_change_draft_saved', 'flow_change_validation_blocked',
        'flow_change_validation_rejected', 'flow_change_validated',
        'flow_change_approved', 'flow_change_approval_rejected',
        'flow_change_publish_rejected', 'flow_change_published',
        'flow_control_changed', 'flow_change_discarded', 'flow_change_expired',
        'flow_route_created', 'flow_route_moved',
        'flow_created', 'flow_cohort_changed'));

-- ===========================================================================
-- Bloque 2. El item nuevo en el ciclo.
-- ===========================================================================

-- La zona de la bolsa de un flujo nuevo: la de su horario, o la de la
-- instancia que paso operator_publish_flow_change_v1 (con horario: siempre la
-- definicion no la trae; contrato flow-engine-v2, seccion 3).
create function public._flow_change_new_flow_zone(p_definition jsonb)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case when jsonb_typeof(p_definition -> 'horario') = 'object'
                then p_definition #>> '{horario,zona}'
                else nullif(current_setting('flow_change.instance_time_zone', true), '') end
$function$;

-- Lo que impide publicar un item nuevo, o null. Se mira antes de escribir,
-- bajo el lock de los controles de la instancia: lo que la apertura
-- (_flow_engine_open_native_flow) rechazaria vuelve como resultado y no como
-- excepcion.
create function public._flow_change_new_flow_blocker(p_tenant_ref text, p_ally_ref text, p_item jsonb)
returns text
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_definition jsonb;
    v_unsupported text;
    v_pool_key text;
    v_pool public.flow_engine_pools%rowtype;
    v_zone text;
begin
    if not public._flow_store_product_is_bound(p_tenant_ref, p_ally_ref, p_item ->> 'producto') then
        return 'flow_store_product_not_bound';
    end if;
    -- La identidad del control no lleva la aliada (flow_controls_identity).
    if exists (select 1 from public.flow_controls c
                where c.tenant_ref = p_tenant_ref and c.product_ref = p_item ->> 'producto'
                  and c.flow_key = p_item ->> 'flujo') then
        return 'flow_already_registered';
    end if;
    if p_item -> 'prender' is distinct from '"cohorte"'::jsonb then
        return 'new_flow_must_start_in_cohort';
    end if;
    v_definition := (p_item -> 'definicion') || jsonb_build_object('estado', 'publicada', 'version', 1);
    -- El invariante 3 del almacen, como resultado: una plantilla {a_crear: ...}
    -- deja guardar pero no publicar (regla 11).
    if jsonb_path_exists(v_definition, 'strict $.** ? (@ == "a_medir" || @ == "a_decidir")')
       or jsonb_path_exists(v_definition, 'strict $.**.a_crear') then
        return 'flow_definition_unresolved_value';
    end if;
    v_unsupported := public._flow_engine_native_unsupported(v_definition);
    if v_unsupported is not null then
        return v_unsupported;
    end if;
    if exists (select 1 from public.pilot_proactive_contact_caps k where k.tenant_key = p_tenant_ref) then
        return 'flow_route_person_cap_present';
    end if;
    v_zone := public._flow_change_new_flow_zone(v_definition);
    if v_zone is null then
        return 'flow_engine_native_zone_missing';
    end if;
    if not exists (select 1 from pg_timezone_names tz where tz.name = v_zone) then
        return 'flow_engine_pool_zone_invalid';
    end if;
    v_pool_key := public._flow_engine_native_pool(v_definition);
    if exists (select 1 from public.pilot_runtime_controls runtime where runtime.scope_key = v_pool_key)
       or exists (select 1 from public.pilot_scope_versions scope where scope.scope_key = v_pool_key) then
        return 'flow_engine_pool_is_legacy_scope';
    end if;
    select pool.* into v_pool from public.flow_engine_pools pool where pool.pool = v_pool_key;
    if found then
        if v_pool.tenant_ref is distinct from p_tenant_ref or v_pool.ally_ref is distinct from p_ally_ref then
            return 'flow_engine_pool_other_instance';
        end if;
        if v_pool.timezone is distinct from v_zone then
            return 'flow_engine_pool_zone_mismatch';
        end if;
    end if;
    return null;
end;
$function$;

do $patch_cycle$
begin
    -- La quinta clase de item. La forma de la definicion y del OK del cliente
    -- es la de una version: se mira con la misma funcion, sobre el item como
    -- version con un flow_id de mentira y en_curso: seguir.
    perform pg_temp.flow_change_new_flow_patch(
        'public._flow_change_items_are_valid(jsonb)',
        E'        select array_agg(k order by k) into v_keys from jsonb_object_keys(v_item) k;\n'
        || E'        if not coalesce(\n'
        || E'            jsonb_typeof(v_item -> ''flow_id'') = ''string''\n',
        E'        select array_agg(k order by k) into v_keys from jsonb_object_keys(v_item) k;\n'
        || E'        -- flow_change_new_flow: begin\n'
        || E'        -- El item nuevo (ciclo 1.7.0, 20261024000600): sin flow_id, con\n'
        || E'        -- prender; a lo sumo uno por producto y flujo.\n'
        || E'        if v_item ->> ''tipo'' = ''nuevo'' then\n'
        || E'            if not coalesce(\n'
        || E'                v_keys <@ array[''definicion'', ''flujo'', ''ok_del_cliente'', ''prender'', ''producto'', ''tipo'']\n'
        || E'                and v_keys @> array[''definicion'', ''flujo'', ''prender'', ''producto'', ''tipo'']\n'
        || E'                and jsonb_typeof(v_item -> ''prender'') = ''string''\n'
        || E'                and public._flow_change_items_are_valid(jsonb_build_array(\n'
        || E'                    (v_item - ''prender'') || jsonb_build_object(''tipo'', ''version'',\n'
        || E'                        ''flow_id'', ''00000000-0000-0000-0000-000000000000'', ''en_curso'', ''seguir''))), false) then\n'
        || E'                return false;\n'
        || E'            end if;\n'
        || E'            if (''nuevo:'' || (v_item ->> ''producto'') || ''/'' || (v_item ->> ''flujo'')) = any (v_seen) then\n'
        || E'                return false;\n'
        || E'            end if;\n'
        || E'            v_seen := v_seen || (''nuevo:'' || (v_item ->> ''producto'') || ''/'' || (v_item ->> ''flujo''));\n'
        || E'            continue;\n'
        || E'        end if;\n'
        || E'        -- flow_change_new_flow: end\n'
        || E'        if not coalesce(\n'
        || E'            jsonb_typeof(v_item -> ''flow_id'') = ''string''\n',
        'flow_change_new_flow: begin');

    -- Un item nuevo no tiene flow_id: no entra en la base ni en la generacion.
    perform pg_temp.flow_change_new_flow_patch(
        'public._flow_change_item_flow_ids(jsonb)',
        E'      from jsonb_array_elements(p_items) item\n',
        E'      from jsonb_array_elements(p_items) item\n'
        || E'     where item ? ''flow_id'' -- flow_change_new_flow: un item nuevo no lleva flow_id\n',
        'flow_change_new_flow: un item nuevo');

    -- Guardar el borrador de un item nuevo: el producto de la instancia, la
    -- clave libre y prender: cohorte. No hay control que leer.
    perform pg_temp.flow_change_new_flow_patch(
        'public._flow_change_save_draft(text,text,uuid,integer,jsonb,jsonb,text,text,text)',
        E'    for v_item in select value from jsonb_array_elements(p_items) loop\n'
        || E'        select * into v_control from public.flow_controls c\n',
        E'    for v_item in select value from jsonb_array_elements(p_items) loop\n'
        || E'        -- flow_change_new_flow: begin\n'
        || E'        if v_item ->> ''tipo'' = ''nuevo'' then\n'
        || E'            if v_item -> ''prender'' is distinct from ''"cohorte"''::jsonb then\n'
        || E'                raise exception using errcode = ''22023'', message = ''new_flow_must_start_in_cohort'';\n'
        || E'            end if;\n'
        || E'            if not public._flow_store_product_is_bound(p_tenant_ref, p_ally_ref, v_item ->> ''producto'') then\n'
        || E'                raise exception using errcode = ''55000'', message = ''flow_store_product_not_bound'';\n'
        || E'            end if;\n'
        || E'            if exists (select 1 from public.flow_controls c\n'
        || E'                        where c.tenant_ref = p_tenant_ref and c.product_ref = v_item ->> ''producto''\n'
        || E'                          and c.flow_key = v_item ->> ''flujo'') then\n'
        || E'                return jsonb_build_object(''ok'', false, ''reason_code'', ''flow_already_registered'');\n'
        || E'            end if;\n'
        || E'            continue;\n'
        || E'        end if;\n'
        || E'        -- flow_change_new_flow: end\n'
        || E'        select * into v_control from public.flow_controls c\n',
        'flow_change_new_flow: begin');

    -- Publicar: las variables de los items nuevos.
    perform pg_temp.flow_change_new_flow_patch(
        'public._flow_change_publish(text,text,uuid,text,jsonb)',
        E'    v_published jsonb := ''[]''::jsonb;\nbegin\n',
        E'    v_published jsonb := ''[]''::jsonb;\n'
        || E'    -- flow_change_new_flow: las variables del item nuevo.\n'
        || E'    v_new jsonb;\n'
        || E'    v_new_blocker text;\n'
        || E'    v_new_flow_id uuid;\n'
        || E'    v_open jsonb;\n'
        || E'begin\n',
        'flow_change_new_flow: las variables');

    -- Publicar: lo que impide abrir un item nuevo, antes de escribir nada.
    perform pg_temp.flow_change_new_flow_patch(
        'public._flow_change_publish(text,text,uuid,text,jsonb)',
        E'    -- Las escrituras, por flujo y en orden de flow_id: primero el contenido y\n',
        E'    -- flow_change_new_flow: begin (los chequeos)\n'
        || E'    -- Un item nuevo que no se puede abrir no escribe nada: el motivo vuelve\n'
        || E'    -- como resultado y queda su evento.\n'
        || E'    for v_new in select item from jsonb_array_elements(v_proposal.items) item\n'
        || E'                 where item ->> ''tipo'' = ''nuevo'' loop\n'
        || E'        v_new_blocker := public._flow_change_new_flow_blocker(p_tenant_ref, p_ally_ref, v_new);\n'
        || E'        if v_new_blocker is not null then\n'
        || E'            perform public._flow_change_event(v_proposal, ''flow_change_publish_rejected'',\n'
        || E'                v_proposal.approved_by_kind, v_proposal.approved_by_ref, v_new_blocker,\n'
        || E'                jsonb_build_object(''producto'', v_new ->> ''producto'', ''flujo'', v_new ->> ''flujo''));\n'
        || E'            return jsonb_build_object(''ok'', false, ''reason_code'', v_new_blocker);\n'
        || E'        end if;\n'
        || E'    end loop;\n'
        || E'    -- flow_change_new_flow: end (los chequeos)\n'
        || E'    -- Las escrituras, por flujo y en orden de flow_id: primero el contenido y\n',
        'flow_change_new_flow: begin (los chequeos)');

    -- Publicar: registrar, publicar la v1 y abrir en cohorte, con la propuesta
    -- ya publicada (la apertura lo exige) y en la misma transaccion.
    perform pg_temp.flow_change_new_flow_patch(
        'public._flow_change_publish(text,text,uuid,text,jsonb)',
        E'       set status = ''publicada'', published_at = clock_timestamp()\n'
        || E'     where id = v_proposal.id;\n',
        E'       set status = ''publicada'', published_at = clock_timestamp()\n'
        || E'     where id = v_proposal.id;\n'
        || E'    -- flow_change_new_flow: begin (registrar y abrir)\n'
        || E'    for v_new in select item from jsonb_array_elements(v_proposal.items) item\n'
        || E'                 where item ->> ''tipo'' = ''nuevo'' loop\n'
        || E'        -- 2. Registrar el control, atribuido a quien aprobo y con el para que.\n'
        || E'        insert into public.flow_controls (tenant_ref, ally_ref, product_ref, flow_key, state,\n'
        || E'            active_version, generation, change_kind, changed_by_kind, changed_by_ref, change_reason)\n'
        || E'        values (p_tenant_ref, p_ally_ref, v_new ->> ''producto'', v_new ->> ''flujo'', ''apagado'',\n'
        || E'            null, 0, ''registrado'', v_proposal.approved_by_kind, v_proposal.approved_by_ref,\n'
        || E'            v_proposal.purpose)\n'
        || E'        returning flow_id into v_new_flow_id;\n'
        || E'        -- 3. La v1 (definition_sha256 lo calcula el disparador) y el control en ella.\n'
        || E'        insert into public.flow_definitions (flow_id, version, schema_ref, definition,\n'
        || E'            definition_sha256, proposal_id, approved_by_kind, approved_by_ref)\n'
        || E'        values (v_new_flow_id, 1, ''setter-flujo/v1'',\n'
        || E'            (v_new -> ''definicion'') || jsonb_build_object(''estado'', ''publicada'', ''version'', 1),\n'
        || E'            repeat(''0'', 64), v_proposal.id, v_proposal.approved_by_kind, v_proposal.approved_by_ref);\n'
        || E'        update public.flow_controls\n'
        || E'           set active_version = 1, generation = generation + 1, change_kind = ''publicado'',\n'
        || E'               changed_by_kind = v_proposal.approved_by_kind, changed_by_ref = v_proposal.approved_by_ref,\n'
        || E'               changed_by_proposal_id = v_proposal.id, change_reason = v_proposal.purpose,\n'
        || E'               changed_at = clock_timestamp()\n'
        || E'         where flow_id = v_new_flow_id;\n'
        || E'        -- 4 a 6. La bolsa, la ruta nativa y el control en cohorte (20261024000200).\n'
        || E'        v_open := public._flow_engine_open_native_flow(v_new_flow_id, v_proposal.id,\n'
        || E'            public._flow_change_new_flow_zone(v_new -> ''definicion''));\n'
        || E'        if v_open -> ''ok'' is distinct from ''true''::jsonb then\n'
        || E'            raise exception using errcode = ''55000'',\n'
        || E'                message = coalesce(v_open ->> ''reason_code'', ''flow_engine_native_open_invalid'');\n'
        || E'        end if;\n'
        || E'        -- 7. flow_created.\n'
        || E'        insert into public.flow_change_events (tenant_ref, ally_ref, proposal_id, flow_id, event_type,\n'
        || E'            actor_kind, actor_ref, reason_code, generation, data)\n'
        || E'        values (p_tenant_ref, p_ally_ref, v_proposal.id, v_new_flow_id, ''flow_created'',\n'
        || E'            v_proposal.approved_by_kind, v_proposal.approved_by_ref, ''flow_created'',\n'
        || E'            (v_open ->> ''generation'')::bigint,\n'
        || E'            jsonb_build_object(''producto'', v_new ->> ''producto'', ''flujo'', v_new ->> ''flujo'',\n'
        || E'                ''version'', 1, ''pool'', v_open ->> ''pool'', ''state'', ''cohorte''));\n'
        || E'        v_summary := v_summary || jsonb_build_array(jsonb_build_object(''flow_id'', v_new_flow_id,\n'
        || E'            ''from_version'', null, ''to_version'', 1, ''from_state'', null, ''to_state'', ''cohorte'',\n'
        || E'            ''en_curso'', null, ''nuevo'', true));\n'
        || E'        v_published := v_published || jsonb_build_array(jsonb_build_object(\n'
        || E'            ''producto'', v_new ->> ''producto'', ''flujo'', v_new ->> ''flujo'', ''version'', 1,\n'
        || E'            ''state'', ''cohorte'', ''generation'', (v_open ->> ''generation'')::bigint,\n'
        || E'            ''flow_id'', v_new_flow_id, ''pool'', v_open ->> ''pool''));\n'
        || E'    end loop;\n'
        || E'    -- flow_change_new_flow: end (registrar y abrir)\n',
        'flow_change_new_flow: begin (registrar y abrir)');

    -- La zona de la instancia para la apertura (con horario: siempre).
    perform pg_temp.flow_change_new_flow_patch(
        'public.operator_publish_flow_change_v1(jsonb,uuid,text,jsonb)',
        E'    v_instance := public._flow_store_instance(p_instance);\n',
        E'    v_instance := public._flow_store_instance(p_instance);\n'
        || E'    -- flow_change_new_flow: la zona de la instancia, para la bolsa de un flujo\n'
        || E'    -- nuevo con horario: siempre (la valida la apertura). Local a la transaccion.\n'
        || E'    perform set_config(''flow_change.instance_time_zone'',\n'
        || E'        case when jsonb_typeof(p_instance -> ''time_zone'') = ''string''\n'
        || E'             then left(p_instance ->> ''time_zone'', 64) else '''' end, true);\n',
        'flow_change_new_flow: la zona');

    -- El almacen dice que flujo es nativo: el validador mira la 14 con el motor
    -- nativo y el comando del operador sabe a cual se le carga una cohorte.
    perform pg_temp.flow_change_new_flow_patch(
        'public.read_flow_store_v1(jsonb)',
        E'                ''changed_at'', c.changed_at, ''changed_by_kind'', c.changed_by_kind,\n',
        E'                ''changed_at'', c.changed_at, ''changed_by_kind'', c.changed_by_kind,\n'
        || E'                -- flow_change_new_flow: native\n'
        || E'                ''native'', exists (select 1 from public.flow_engine_routes r\n'
        || E'                                   where r.flow_id = c.flow_id and r.legacy_kind is null),\n',
        'flow_change_new_flow: native');
end;
$patch_cycle$;

-- ===========================================================================
-- Bloque 3. La cohorte del operador.
-- ===========================================================================

-- Agrega o saca un contacto de la cohorte de prueba de un flujo nativo
-- (contrato flow-engine-v2, 7.3). El telefono lo lee el comando de la
-- terminal; aca se resuelve a su contacto por la forma canonica de WhatsApp y
-- no sale en la respuesta, en un evento ni en un mensaje de error. Hasta 5
-- contactos por flujo. Solo el duenio la ejecuta, como las otras operator_*.
create function public.operator_flow_cohort_v1(
    p_instance jsonb, p_product_ref text, p_flow_key text, p_action text, p_phone text,
    p_actor_name text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    c_max_members constant integer := 5;
    v_instance record;
    v_control public.flow_controls%rowtype;
    v_canonical text;
    v_contacts uuid[];
    v_changed boolean := false;
    v_members integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_action in ('agregar', 'sacar'), false) then
        raise exception using errcode = '22023', message = 'flow_cohort_action_invalid';
    end if;
    if not coalesce(public._flow_store_actor_is_valid('operador', p_actor_name), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_control from public.flow_controls c
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.product_ref = p_product_ref and c.flow_key = p_flow_key
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_not_found';
    end if;
    if not exists (select 1 from public.flow_engine_routes r
                    where r.flow_id = v_control.flow_id and r.legacy_kind is null) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_needs_native_flow');
    end if;
    v_canonical := public._whatsapp_phone_canonical(p_phone);
    if v_canonical is null or v_canonical !~ '^[0-9]{8,15}$' then
        raise exception using errcode = '22023', message = 'flow_cohort_phone_invalid';
    end if;
    select array_agg(distinct contact_row.id) into v_contacts
      from public.contacts contact_row
     where public._whatsapp_phone_canonical(contact_row.phone) = v_canonical;
    if coalesce(cardinality(v_contacts), 0) = 0 then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_contact_not_found');
    end if;
    if cardinality(v_contacts) > 1 then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_contact_ambiguous');
    end if;
    if p_action = 'agregar' then
        if not exists (select 1 from public.flow_cohort_members member
                        where member.flow_id = v_control.flow_id and member.contact_id = v_contacts[1]) then
            if (select count(*) from public.flow_cohort_members member
                 where member.flow_id = v_control.flow_id) >= c_max_members then
                return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_full');
            end if;
            insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref)
            values (v_control.flow_id, v_contacts[1], p_actor_name);
            v_changed := true;
        end if;
    else
        delete from public.flow_cohort_members member
         where member.flow_id = v_control.flow_id and member.contact_id = v_contacts[1];
        v_changed := found;
        if not v_changed then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_member_not_found');
        end if;
    end if;
    select count(*)::integer into v_members from public.flow_cohort_members member
     where member.flow_id = v_control.flow_id;
    if v_changed then
        insert into public.flow_change_events (tenant_ref, ally_ref, flow_id, event_type, actor_kind,
            actor_ref, reason_code, data)
        values (v_control.tenant_ref, v_control.ally_ref, v_control.flow_id, 'flow_cohort_changed',
            'operador', p_actor_name, 'flow_cohort_' || p_action,
            jsonb_build_object('action', p_action, 'members', v_members,
                'reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    end if;
    return jsonb_build_object('ok', true, 'action', p_action, 'changed', v_changed, 'members', v_members,
        'max_members', c_max_members);
end;
$function$;

-- ===========================================================================
-- Bloque 4. La simulacion de un flujo nuevo.
-- ===========================================================================

-- Cuantas compras aprobadas del producto llegaron desde p_since, y de cuantas
-- personas (por mail, o por el telefono si no hay mail): la simulacion de un
-- item nuevo con el disparador compra (contrato flow-engine-v2, seccion 14).
-- Es una estimacion con los eventos reales y no trae nada de una persona. La
-- ventana es la de la historia del motor: hasta 8 dias.
create function public.read_flow_engine_native_trigger_counts_v1(
    p_instance jsonb, p_product_ref text, p_trigger text, p_since timestamptz)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_product_ids text[];
    v_events integer;
    v_people integer;
    v_now timestamptz := clock_timestamp();
begin
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_product_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$', false) then
        raise exception using errcode = '22023', message = 'flow_store_flow_invalid';
    end if;
    if not public._flow_store_product_is_bound(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_ref) then
        raise exception using errcode = '22023', message = 'flow_store_product_not_bound';
    end if;
    if p_trigger is distinct from 'compra' then
        raise exception using errcode = '22023', message = 'flow_engine_native_trigger_invalid';
    end if;
    if p_since is null or p_since > v_now then
        raise exception using errcode = '22023', message = 'flow_engine_history_since_invalid';
    end if;
    if p_since < v_now - interval '8 days' then
        raise exception using errcode = '22023', message = 'flow_engine_history_window_too_wide';
    end if;
    select coalesce(array_agg(distinct b.hotmart_product_id::text), array[]::text[]) into v_product_ids
      from public.commercial_ally_runtime_bindings b
     where b.tenant_ref = v_instance.o_tenant_ref and b.ally_ref = v_instance.o_ally_ref
       and b.funnel_ref = p_product_ref and b.hotmart_product_id is not null;
    select count(distinct coalesce(e.payload #>> '{data,purchase,transaction}', e.id::text))::integer,
           count(distinct coalesce(nullif(lower(btrim(e.payload #>> '{data,buyer,email}')), ''),
                                   public._whatsapp_phone_canonical(e.payload #>> '{data,buyer,checkout_phone}'),
                                   e.id::text))::integer
      into v_events, v_people
      from public.webhook_events e
     where e.source = 'hotmart' and e.event_type = 'PURCHASE_APPROVED'
       and e.received_at >= p_since
       and e.payload #>> '{data,product,id}' = any (v_product_ids);
    return jsonb_build_object('contract', 'flow-engine-v2', 'trigger', p_trigger, 'product_ref', p_product_ref,
        'since', p_since, 'until', v_now, 'events', v_events, 'people', v_people);
end;
$function$;

-- ===========================================================================
-- Los permisos: ninguno para la API; la lectura de la simulacion, para
-- service_role y para el rol del agente.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._flow_change_new_flow_zone(jsonb)',
        'public._flow_change_new_flow_blocker(text,text,jsonb)',
        'public.operator_flow_cohort_v1(jsonb,text,text,text,text,text,text)',
        'public.read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamptz)'
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
        grant execute on function public.read_flow_engine_native_trigger_counts_v1(jsonb, text, text, timestamptz)
            to service_role;
    end if;
    -- El rol de la T4 lo crea 20261021000100; una base sin el no lo nombra.
    if to_regrole('automation_expert_change') is not null then
        grant execute on function public.read_flow_engine_native_trigger_counts_v1(jsonb, text, text, timestamptz)
            to automation_expert_change;
    end if;
end;
$acl$;

commit;
