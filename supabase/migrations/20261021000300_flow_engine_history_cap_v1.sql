-- flow-engine-history-cap-v1: el tope de read_flow_engine_history_v1 pasa de
-- 1.000.000 a 4.000.000 bytes.
-- Contrato: docs/contracts/flow-engine-v1.md, seccion 2 (la lectura de la
-- historia).
--
-- Por que: la vista previa del ciclo pide 8 dias de historia
-- (HISTORY_WINDOW en src/bridge/flow_change_api.py), y la del carrito de ATT1
-- pasa el tope de 1 MB a partir de los 4 dias. Medido en att1-db en solo
-- lectura: 1 dia 584 KB, 2 dias 744 KB, 3 dias 970 KB (sesion C, 22/10
-- 00:20Z); 8 dias da flow_engine_history_too_large en 3,4 s (sesion B, 22/10).
-- Con eso la simulacion sale siempre failed y la T4 no puede registrar
-- simulation: built.
--
-- Por que 4.000.000: el bridge corta la respuesta del RPC en 4 MiB
-- (MAX_STORE_BYTES = 4 * 1024 * 1024 = 4.194.304, flow_change_api.py). Un tope
-- de la base por debajo de ese corte conserva el motivo con nombre
-- (flow_engine_history_too_large) en vez de un error generico del bridge.
-- Deja unas 1,6 a 2 veces de margen sobre los 8 dias de hoy (estimados en
-- 2 a 2,5 MB). El tiempo no cambia: la funcion arma la captura entera antes
-- de medirla, asi que los 3,4 s de hoy ya son los de 8 dias, debajo de los
-- 10 s del cliente del bridge y de los 20 s de su statement_timeout.
--
-- Como: solo cambia la constante, con el metodo de 20261021000200 (sobre
-- pg_get_functiondef, un ancla que aparece una sola vez). create or replace
-- conserva el duenio y los permisos, y la migracion verifica las dos cosas.
-- No cambia nada de lo que corre: solo la lectura de la historia, que hoy usa
-- unicamente la vista previa de la T4.

begin;

set local lock_timeout = '5s';

do $cap$
declare
    c_signature constant text := 'public.read_flow_engine_history_v1(jsonb,text,timestamp with time zone)';
    c_anchor constant text := E'    c_max_bytes constant integer := 1000000;\n';
    c_replacement constant text := E'    c_max_bytes constant integer := 4000000; -- flow_engine_history_cap_v1\n';
    v_definition text;
    v_acl_before text;
    v_acl_after text;
begin
    if to_regprocedure(c_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_history_function_missing';
    end if;
    if (select p.proowner from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_history_owner_mismatch';
    end if;
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, c_anchor, '')) <> length(c_anchor)
       or position('flow_engine_history_cap_v1' in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_history_definition';
    end if;
    select coalesce(p.proacl::text, '') into v_acl_before
      from pg_proc p where p.oid = c_signature::regprocedure;

    execute replace(v_definition, c_anchor, c_replacement);

    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if position(c_replacement in v_definition) = 0 or position(c_anchor in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_history_cap_not_replaced';
    end if;
    select coalesce(p.proacl::text, '') into v_acl_after
      from pg_proc p where p.oid = c_signature::regprocedure;
    if v_acl_after is distinct from v_acl_before then
        raise exception using errcode = '55000', message = 'flow_engine_history_acl_changed';
    end if;
end;
$cap$;

commit;
