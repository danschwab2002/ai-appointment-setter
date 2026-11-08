-- flow-store-route-executor-v1: el almacen dice quien ejecuta cada flujo (E-353).
-- Contrato: docs/contracts/flow-change-cycle-v1.md (1.11.0, «Lo que se publicaria
-- sin efecto»).
--
-- Por que. El validador no sabia si un flujo del legado lo manda el motor o el
-- legado: con la ruta en legado, una version nueva se publicaba y no cambiaba
-- ningun envio (E-198). read_flow_store_v1 solo daba native.
--
-- Que cambia. Una clave mas por flujo en read_flow_store_v1: route_executor, con
-- el executor de flow_engine_routes (motor o legado), o null si el flujo no tiene
-- ruta. No toca tablas, filas, permisos ni ninguna otra funcion; no cambia como
-- se publica ni que se manda. Quien lea el almacen sin mirar la clave sigue igual.
--
-- La vuelta atras es restaurar la definicion anterior de la funcion: el bridge
-- trata la clave ausente como «no se sabe» y no bloquea por eso.
begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $patch$
declare
    v_signature constant text := 'public.read_flow_store_v1(jsonb)';
    v_anchor constant text :=
        E'where r.flow_id = c.flow_id and r.legacy_kind is null),\n';
    v_marker constant text := 'flow_store_route_executor';
    v_definition text;
    v_acl_before text;
begin
    if to_regprocedure(v_signature) is null then
        raise exception using errcode = '55000', message = 'flow_store_route_executor_function_missing';
    end if;
    if (select p.proowner from pg_proc p where p.oid = v_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_store_route_executor_owner_mismatch';
    end if;
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if position(v_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'flow_store_route_executor_already_applied';
    end if;
    if length(v_definition) - length(replace(v_definition, v_anchor, '')) <> length(v_anchor) then
        raise exception using errcode = '55000', message = 'unexpected_flow_store_definition';
    end if;
    select coalesce(p.proacl::text, '') into v_acl_before from pg_proc p where p.oid = v_signature::regprocedure;
    execute replace(v_definition, v_anchor, v_anchor
        || E'                -- flow_store_route_executor: quien ejecuta el flujo; null sin ruta (E-353)\n'
        || E'                ''route_executor'', (select r.executor from public.flow_engine_routes r\n'
        || E'                                      where r.flow_id = c.flow_id),\n');
    if position(v_marker in pg_get_functiondef(v_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_store_route_executor_not_replaced';
    end if;
    -- create or replace conserva el duenio y los permisos: se verifica.
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = v_signature::regprocedure)
       is distinct from v_acl_before then
        raise exception using errcode = '55000', message = 'flow_store_route_executor_acl_changed';
    end if;
end;
$patch$;

commit;
