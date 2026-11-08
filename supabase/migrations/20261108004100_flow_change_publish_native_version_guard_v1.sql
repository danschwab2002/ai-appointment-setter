-- flow-change-publish-native-version-guard-v1: la base no publica una version de un flujo
-- nativo que el motor nativo no ejecuta (E-357, P28).
-- Contrato: docs/contracts/flow-change-cycle-v1.md (1.12.0, «El Automation Expert cambia la
-- estructura de un flujo nativo»).
--
-- Por que. Desde P28 el Automation Expert puede cambiar la estructura de un nativo (repeticion,
-- pasos dentro de la regla 14, prioridad). El validador mira la regla 14, pero la base no la volvia
-- a mirar en una version: si el validador y la base difieren, la version se publica y despues
-- ninguna admision arranca (motor_no_arranca). Es lo que ya hace la base con un item nuevo
-- (_flow_change_new_flow_blocker): el mismo chequeo, con la misma funcion, para una version y para
-- el destino de un volver de un flujo nativo.
--
-- Que cambia. Un reemplazo contado en _flow_change_publish, antes de las escrituras: si el flujo
-- es nativo (ruta sin legacy_kind) y _flow_engine_native_unsupported dice algo de la definicion que
-- quedaria activa, la publicacion vuelve con ok:false y ese motivo, y queda su evento. Un flujo del
-- legado, con o sin ruta del motor, no entra: su motor es otro. No toca tablas, filas ni permisos,
-- y no cambia nada de lo que ya corre: una version que el motor ejecuta se publica igual.
--
-- La vuelta atras es restaurar la definicion anterior de la funcion.
begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $patch$
declare
    v_signature constant text := 'public._flow_change_publish(text,text,uuid,text,jsonb)';
    v_anchor constant text := E'    -- flow_change_new_flow: begin (los chequeos)\n';
    v_marker constant text := 'flow_change_publish_native_version_guard';
    v_definition text;
    v_acl_before text;
begin
    if to_regprocedure(v_signature) is null or to_regprocedure('public._flow_engine_native_unsupported(jsonb)') is null then
        raise exception using errcode = '55000', message = 'flow_change_publish_native_version_guard_function_missing';
    end if;
    if (select p.proowner from pg_proc p where p.oid = v_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_change_publish_native_version_guard_owner_mismatch';
    end if;
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if position(v_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'flow_change_publish_native_version_guard_already_applied';
    end if;
    if length(v_definition) - length(replace(v_definition, v_anchor, '')) <> length(v_anchor) then
        raise exception using errcode = '55000', message = 'unexpected_flow_change_publish_definition';
    end if;
    select coalesce(p.proacl::text, '') into v_acl_before from pg_proc p where p.oid = v_signature::regprocedure;
    execute replace(v_definition, v_anchor,
        E'    -- flow_change_publish_native_version_guard: begin (E-357)\n'
        || E'    -- Una version de un nativo que el motor no ejecuta no se publica: despues no arrancaria nunca.\n'
        || E'    for v_new in select item from jsonb_array_elements(v_proposal.items) item\n'
        || E'                 where item ->> ''tipo'' in (''version'', ''volver'')\n'
        || E'                   and exists (select 1 from public.flow_engine_routes r\n'
        || E'                                where r.flow_id = (item ->> ''flow_id'')::uuid and r.legacy_kind is null) loop\n'
        || E'        v_new_blocker := public._flow_engine_native_unsupported(\n'
        || E'            case v_new ->> ''tipo'' when ''version'' then v_new -> ''definicion''\n'
        || E'                 else (select d.definition from public.flow_definitions d\n'
        || E'                        where d.flow_id = (v_new ->> ''flow_id'')::uuid\n'
        || E'                          and d.version = (v_new ->> ''version'')::integer) end);\n'
        || E'        if v_new_blocker is not null then\n'
        || E'            perform public._flow_change_event(v_proposal, ''flow_change_publish_rejected'',\n'
        || E'                v_proposal.approved_by_kind, v_proposal.approved_by_ref, v_new_blocker,\n'
        || E'                jsonb_build_object(''producto'', v_new ->> ''producto'', ''flujo'', v_new ->> ''flujo''));\n'
        || E'            return jsonb_build_object(''ok'', false, ''reason_code'', v_new_blocker);\n'
        || E'        end if;\n'
        || E'    end loop;\n'
        || E'    -- flow_change_publish_native_version_guard: end\n'
        || v_anchor);
    if position(v_marker in pg_get_functiondef(v_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_change_publish_native_version_guard_not_replaced';
    end if;
    -- create or replace conserva el duenio y los permisos: se verifica.
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = v_signature::regprocedure)
       is distinct from v_acl_before then
        raise exception using errcode = '55000', message = 'flow_change_publish_native_version_guard_acl_changed';
    end if;
end;
$patch$;

commit;
