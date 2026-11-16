-- flow-audience-reach-v1: cuantas personas cumplen hoy la audiencia de un flujo
-- con disparo por audiencia (fecha, cada, manual), sin datos de nadie (E-622,
-- macro 2 del mapa «el AE maneja todos los flujos»).
-- Contratos: docs/contracts/flow-audience-trigger-v1.md y flow-change-cycle-v1.md.
--
-- Por que. E-621 dejo que un flujo nuevo de audiencia se publique para la
-- cohorte. Pasarlo a todos (un item prender) o apagarlo pedia la simulacion de
-- un flujo nativo, que cuenta los disparos del evento del flujo
-- (read_flow_engine_native_trigger_counts_v1); un disparo por audiencia no tiene
-- evento, y la simulacion quedaba failed con
-- simulation_native_flow_counts_invalid: la pagina no publicaba el prender y
-- solo lo hacia el operador con --sin-simulacion. Lo que hace falta es el
-- numero real de la audiencia, y la unica lectura que lo tiene
-- (read_flow_audience_candidates_v1, 20261112220000) es del bridge, con
-- telefonos.
--
-- 1. Una funcion nueva, solo para el rol del agente (automation_expert_change):
--    read_flow_audience_reach_v1(p_instance, p_flow_id, p_version). Lee la
--    audiencia posible del flujo con read_flow_audience_candidates_v1 (la misma
--    definicion de «compro» y del formulario que la lista y el armado) y la
--    devuelve contada: candidatas, con baja, ya clientas (prospecta), las que
--    cumplen hoy, sin contacto y ambiguas. Sin telefonos, contactos ni nombres.
--    Las etiquetas de Chatwoot (para.etiquetas, para.excluye) y la repeticion no
--    las ve la base: el numero es un maximo y el que la usa lo dice.
-- 2. El operador la lee con psql como duenio de la base: no se le da a
--    service_role ni a la API.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente. Sin un bridge
-- que la llame no corre nunca. Correrla dos veces aborta con 55000 sin cambiar
-- nada.
--
-- Errores: los de read_flow_audience_candidates_v1 (22023 entrada invalida o no
-- es un flujo de audiencia, P0002 el flujo o la version activa no existen).

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.read_flow_audience_candidates_v1(jsonb,uuid,integer,integer)') is null
       or to_regprocedure('public._flow_audience_active(jsonb,uuid,integer)') is null then
        raise exception using errcode = '55000', message = 'flow_audience_reach_prerequisite_missing';
    end if;
    if to_regprocedure('public.read_flow_audience_reach_v1(jsonb,uuid,integer)') is not null then
        raise exception using errcode = '55000', message = 'flow_audience_reach_already_applied';
    end if;
end;
$already$;

-- Cuantas personas cumplen hoy el «para» de la version activa de un flujo de
-- audiencia. La lectura de candidatas se pide con su tope (20000): si lo alcanza,
-- read_capped lo dice y el numero es un minimo de lo que hay.
create function public.read_flow_audience_reach_v1(p_instance jsonb, p_flow_id uuid, p_version integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    c_read_cap constant integer := 20000;
    v_state text;
    v_tenant text;
    v_product text;
    v_definition jsonb;
    v_status text;
    v_buyer_product text;
    v_read jsonb;
    v_candidates integer;
    v_opted_out integer;
    v_customer integer;
    v_matching integer;
    v_without_contact integer;
    v_ambiguous integer;
begin
    if p_flow_id is null or p_version is null then
        raise exception using errcode = '22023', message = 'flow_audience_read_invalid';
    end if;
    select a.o_state, a.o_tenant_ref, a.o_product_ref, a.o_definition
      into v_state, v_tenant, v_product, v_definition
      from public._flow_audience_active(p_instance, p_flow_id, p_version) a;
    -- La misma lectura que la lista: valida la forma, el estado y la ventana del formulario.
    v_read := public.read_flow_audience_candidates_v1(p_instance, p_flow_id, p_version, c_read_cap);
    v_status := v_definition #>> '{para,es}';
    -- «Compro» como flow_audience.select_members: el producto del para, o el de la definicion; sin
    -- ninguno, cualquier compra.
    v_buyer_product := coalesce(v_definition #>> '{para,producto}', v_definition ->> 'producto');

    select count(*)::integer,
           count(*) filter (where (c.value ->> 'opted_out')::boolean)::integer,
           count(*) filter (where not (c.value ->> 'opted_out')::boolean and c.is_customer)::integer,
           count(*) filter (where not (c.value ->> 'opted_out')::boolean
                              and c.is_customer = (v_status = 'clienta')
                              and (v_status = 'clienta' or nullif(c.value ->> 'form_at', '') is not null))::integer
      into v_candidates, v_opted_out, v_customer, v_matching
      from (
          select candidate.value,
                 case when v_buyer_product is null
                      then jsonb_array_length(coalesce(candidate.value -> 'customer_products', '[]'::jsonb)) > 0
                      else coalesce(candidate.value -> 'customer_products', '[]'::jsonb) ? v_buyer_product
                 end as is_customer
            from jsonb_array_elements(coalesce(v_read -> 'candidates', '[]'::jsonb)) candidate
      ) c;
    v_without_contact := coalesce((v_read ->> 'sin_contacto')::integer, 0);
    v_ambiguous := coalesce((v_read ->> 'ambiguas')::integer, 0);

    return jsonb_build_object(
        'contract', 'flow-audience-reach-v1',
        'flow_id', p_flow_id,
        'version', p_version,
        'product_ref', v_product,
        'state', v_state,
        'status', v_status,
        'measured_at', clock_timestamp(),
        'candidates', v_candidates,
        'opted_out', v_opted_out,
        'already_customer', case when v_status = 'prospecta' then v_customer else 0 end,
        'matching', v_matching,
        'without_contact', v_without_contact,
        'ambiguous', v_ambiguous,
        'read_capped', v_candidates + v_without_contact + v_ambiguous >= c_read_cap);
end;
$function$;

-- ===========================================================================
-- Los permisos: ninguno para la API ni para service_role; la lectura, para el
-- rol del agente.
-- ===========================================================================

do $acl$
declare
    v_role text;
begin
    revoke all on function public.read_flow_audience_reach_v1(jsonb, uuid, integer) from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function public.read_flow_audience_reach_v1(jsonb, uuid, integer) from %I',
                       v_role);
    end loop;
    -- El rol de la T4 lo crea 20261021000100; una base sin el no lo nombra.
    if to_regrole('automation_expert_change') is not null then
        grant execute on function public.read_flow_audience_reach_v1(jsonb, uuid, integer)
            to automation_expert_change;
    end if;
end;
$acl$;

commit;
