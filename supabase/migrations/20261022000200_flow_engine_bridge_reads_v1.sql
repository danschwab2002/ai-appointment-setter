-- flow-engine-bridge-reads-v1: lo que el bridge de la T2.4 necesita leer y
-- marcar del motor de flujos. Contrato: docs/contracts/flow-engine-v1.md,
-- seccion 7.4. Plan: «T2.4: el plan medido contra el codigo» (2026-10-22, en el
-- vault), commit 1.
--
-- Las tablas del motor no tienen permisos para la API (20261021000200, el
-- bloque de ACL), asi que el bridge las lee por tres funciones nuevas:
--
-- 1. read_flow_engine_routes_v1(p_instance): las rutas de los flujos de la
--    instancia, sin definiciones ni en curso. Es la lectura barata del lote
--    (cada 60 s): read_flow_engine_v1 trae las definiciones y cuenta lo en
--    curso sobre los casos abiertos.
-- 2. read_flow_engine_divergences_v1(p_instance, p_only_unnotified, p_limit):
--    las divergencias de la instancia (el motor no pudo y lo planifico el
--    legado), con el conteo de las que no se avisaron. Sin first_source_ref ni
--    last_source_ref: son ids de evento, y el aviso no los necesita.
-- 3. mark_flow_engine_divergence_notified_v1(p_id, p_notified_at): pone
--    notified_at solo si estaba en null, y devuelve si la cambio. La fila no se
--    puede borrar (flow_engine_divergences_permanent).
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente. Sin la T2.4
-- prendida, nadie la llama. Las tres son security definer, con search_path
-- fijo, y las ejecuta solo service_role.

begin;

do $guard$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regclass('public.flow_engine_routes') is null
       or to_regclass('public.flow_engine_divergences') is null
       or to_regprocedure('public.flow_engine_execution_context_v1(uuid)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_bridge_reads_prerequisite_missing';
    end if;
    if to_regprocedure('public.read_flow_engine_routes_v1(jsonb)') is not null
       or to_regprocedure('public.read_flow_engine_divergences_v1(jsonb,boolean,integer)') is not null
       or to_regprocedure('public.mark_flow_engine_divergence_notified_v1(uuid,timestamptz)') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_bridge_reads_already_applied';
    end if;
end;
$guard$;

-- Las rutas de la instancia, en el orden de read_flow_engine_v1 (producto y
-- flujo). generation sale como numero, igual que en la ruta del contrato.
create function public.read_flow_engine_routes_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_routes jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    select coalesce(jsonb_agg(jsonb_build_object(
               'product_ref', c.product_ref,
               'flow_key', c.flow_key,
               'executor', r.executor,
               'legacy_kind', r.legacy_kind,
               'legacy_scope_key', r.legacy_scope_key,
               'generation', r.generation,
               'flow_state', c.state)
               order by c.product_ref, c.flow_key), '[]'::jsonb)
      into v_routes
      from public.flow_engine_routes r
      join public.flow_controls c on c.flow_id = r.flow_id
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref;
    return jsonb_build_object('routes', v_routes);
end;
$function$;

-- Las divergencias de la instancia, de la mas vieja a la mas nueva. unnotified
-- cuenta todas las de la instancia sin avisar, no solo las de la pagina.
create function public.read_flow_engine_divergences_v1(
    p_instance jsonb, p_only_unnotified boolean, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_unnotified integer;
    v_divergences jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_only_unnotified is null or p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'flow_engine_divergences_parameters_invalid';
    end if;
    select count(*)::integer into v_unnotified
      from public.flow_engine_divergences d
      join public.flow_controls c on c.flow_id = d.flow_id
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and d.notified_at is null;
    select coalesce(jsonb_agg(jsonb_build_object(
               'id', x.id,
               'kind', x.kind,
               'reason', x.reason,
               'flow_key', x.flow_key,
               'product_ref', x.product_ref,
               'version', x.version,
               'count', x.count,
               'first_at', to_char(x.first_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               'last_at', to_char(x.last_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
               order by x.first_at, x.id), '[]'::jsonb)
      into v_divergences
      from (
          select d.id, d.kind, d.reason, c.flow_key, c.product_ref, d.version, d.count, d.first_at, d.last_at
            from public.flow_engine_divergences d
            join public.flow_controls c on c.flow_id = d.flow_id
           where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
             and (not p_only_unnotified or d.notified_at is null)
           order by d.first_at, d.id
           limit p_limit) x;
    return jsonb_build_object('unnotified', v_unnotified, 'divergences', v_divergences);
end;
$function$;

-- La marca del aviso: solo la primera vez. Una divergencia que no existe
-- devuelve false, igual que una ya avisada.
create function public.mark_flow_engine_divergence_notified_v1(p_id uuid, p_notified_at timestamptz)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_id is null or p_notified_at is null then
        raise exception using errcode = '22023', message = 'flow_engine_divergence_mark_parameters_invalid';
    end if;
    update public.flow_engine_divergences
       set notified_at = p_notified_at
     where id = p_id and notified_at is null;
    return found;
end;
$function$;

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public.read_flow_engine_routes_v1(jsonb)',
        'public.read_flow_engine_divergences_v1(jsonb,boolean,integer)',
        'public.mark_flow_engine_divergence_notified_v1(uuid,timestamptz)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
        if to_regrole('service_role') is not null then
            execute format('grant execute on function %s to service_role', v_function);
        end if;
    end loop;
end;
$acl$;

commit;
