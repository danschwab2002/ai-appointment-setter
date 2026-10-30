-- flow-engine-native-legacy-double-case-v1: un carrito nunca abre un caso
-- nativo y uno del legado para la misma persona y el mismo producto (E-204,
-- M1a, el paso P12 del mapa de M16).
--
-- Por que. El caso de un carrito nativo nace con offer_code nulo a proposito
-- (20261024000200; la oferta va al context desde 20261026000200), y cada lado
-- buscaba solo lo suyo:
--   (a) la admision nativa (_flow_engine_admit_native_flow) mira la
--       repeticion y la prioridad solo en flow_engine_runs: con un caso del
--       carrito del legado abierto y un carrito nuevo de la misma persona, el
--       nativo abria un segundo caso y mandaba. El legado, con ese mismo
--       carrito, no manda nada (el planificador del piloto aborta con
--       pilot_case_binding_conflict, o planifica otra oferta como hoy);
--   (b) el planificador del legado (plan_cart_recovery) busca el caso abierto
--       por persona, producto y OFERTA: no ve el caso nativo. Con el nativo
--       pausado o apagado (su vuelta atras) y un recorrido nativo en curso, el
--       siguiente carrito volvia al legado, que abria un segundo caso y
--       mandaba.
--
-- Que cambia. Dos reemplazos contados sobre la definicion VIGENTE:
--
-- 1. _flow_engine_admit_native_flow (20261024000200 + 20261026000200 +
--    20261028000200). Con el disparador carrito, despues de la cohorte y antes
--    del permiso: si la persona tiene abierto (grace_period, active o paused,
--    lo que el planificador del legado llama abierto) un caso que no es nativo
--    (sin fila en flow_engine_native_runs), de la fuente del evento, del producto del
--    flujo (el hotmart_product_id del binding, el mismo con el que nace el caso
--    nativo) y de CUALQUIER oferta, no arranca: motor_no_arranca con
--    caso_del_legado_en_curso y su fila de rechazo (la ve el Automation
--    Expert; no es alertable, no es una falla del motor). El bridge lo suma a
--    NATIVE_CART_UNCLAIMED_REASONS: el carrito vuelve al legado, que hace
--    exactamente lo de hoy (D4 del mapa, por default). La compra (la
--    bienvenida) no cambia: la compra corta los casos abiertos por su cuenta.
--    El contacto ya esta bloqueado por admit_flow_engine_event_v1 (evento y
--    contacto FOR UPDATE), en el mismo orden que el planificador del legado.
--
-- 2. plan_cart_recovery (el planificador base del carrito, debajo del piloto,
--    de plan_cart_recovery_with_identity y de la caida al legado del motor
--    v1). Despues de la repeticion del mismo evento y del lock del contacto,
--    antes de buscar el caso abierto por oferta: si la persona tiene abierto un
--    caso de un carrito NATIVO (policy_key flujo:<id> y
--    context.flow_engine_trigger = carrito, lo que escribe solo la admision
--    nativa) de la misma fuente y el mismo producto, de cualquier oferta, aborta
--    con 55000 pilot_scope_rejected / flujo_nativo_en_curso. El evento queda
--    failed con pilot_scope_rejected:flujo_nativo_en_curso, sin mensaje (como
--    pilot_case_binding_conflict hoy), y la caida del motor v1 lo devuelve como
--    motivo. No suma el carrito al caso nativo: le pondria el binding del
--    piloto. Se mira recovery_cases y no flow_engine_native_runs porque
--    plan_cart_recovery es security invoker y service_role no tiene grants
--    sobre esa tabla (20261024000200): Johanna lo llama sin el piloto.
--
-- Lo que no cambia. Sin un flujo nativo del carrito no hay casos nativos del
-- carrito: la rama (b) nunca da verdadero y la (a) nunca corre (ATT1, con la
-- bienvenida nativa de la compra y el carrito del legado; Johanna). La
-- bienvenida no es un caso del carrito: el carrito del legado de quien tiene
-- la bienvenida abierta planifica como hoy (validate_flow_engine_native_legacy_double_case.mjs).
--
-- Lo que no hace: no crea funciones ni roles, no cambia permisos ni atributos
-- (create or replace conserva el ACL), no toca filas. Correrla dos veces
-- aborta con 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
declare
    v_signature text;
begin
    foreach v_signature in array array[
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
        'public.plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)'] loop
        if to_regprocedure(v_signature) is null then
            raise exception using errcode = '55000', message = 'flow_engine_native_legacy_double_case_function_missing',
                detail = v_signature;
        end if;
        if position('flow_engine_native_legacy_double_case' in pg_get_functiondef(v_signature::regprocedure)) > 0 then
            raise exception using errcode = '55000', message = 'flow_engine_native_legacy_double_case_already_applied',
                detail = v_signature;
        end if;
    end loop;
end;
$already$;

create function pg_temp.flow_engine_native_legacy_double_case_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_legacy_double_case_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_legacy_double_case_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_legacy_double_case_already_applied',
            detail = p_signature;
    end if;
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_legacy_double_case_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_legacy_double_case_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- 1. La admision nativa no abre caso sobre un caso del legado abierto.
-- ===========================================================================

select pg_temp.flow_engine_native_legacy_double_case_patch(
    'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
    $anchor$    -- flow_engine_native_cart_permission: begin
$anchor$,
    $replacement$    -- flow_engine_native_legacy_double_case: begin
    -- El doble caso (20261030000100, D4 del mapa de M16). Con el carrito, un
    -- caso abierto del legado de la persona, de la fuente del evento y del producto del
    -- flujo, de cualquier oferta: el carrito es del legado, que decide como
    -- hoy (el bridge lo trata como sin_reclamo). La compra no pasa por aca.
    if public._flow_engine_native_trigger(v_definition.definition) = 'carrito'
       and exists (
           select 1
             from public.recovery_cases legacy_case
            where legacy_case.contact_id = p_contact_id
              and legacy_case.source = p_event.source
              and legacy_case.external_product_id = (
                  select binding.hotmart_product_id::text
                    from public.commercial_ally_runtime_bindings binding
                   where binding.tenant_ref = p_control.tenant_ref and binding.ally_ref = p_control.ally_ref
                     and binding.funnel_ref = p_control.product_ref and binding.status = 'active'
                   order by binding.binding_version desc
                   limit 1)
              and legacy_case.status in ('grace_period', 'active', 'paused')
              and not exists (
                  select 1 from public.flow_engine_native_runs native_case
                   where native_case.recovery_case_id = legacy_case.id)) then
        perform public._flow_engine_record_start_rejection(
            p_control.flow_id, v_definition.version, p_event.id, 'admision', 'caso_del_legado_en_curso');
        return query select 'motor_no_arranca'::text, 'caso_del_legado_en_curso'::text,
            null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    -- flow_engine_native_legacy_double_case: end

    -- flow_engine_native_cart_permission: begin
$replacement$,
    'flow_engine_native_legacy_double_case');

-- ===========================================================================
-- 2. El planificador del legado ve el caso de un carrito nativo abierto.
-- ===========================================================================

select pg_temp.flow_engine_native_legacy_double_case_patch(
    'public.plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
    $anchor$    select rc.id, rc.status, rc.policy_key, rc.policy_version
      into v_case_id, v_case_status, v_case_policy_key, v_case_policy_version
$anchor$,
    $replacement$    -- flow_engine_native_legacy_double_case: begin
    -- El doble caso, del lado del legado (20261030000100, P12(b) del mapa de
    -- M16): un caso abierto de un carrito nativo de la persona, de la misma
    -- fuente y el mismo producto, de cualquier oferta (nace con offer_code
    -- nulo y la busqueda de abajo no lo ve). Es la vuelta atras del nativo:
    -- pausado o apagado, el carrito vuelve aca y el recorrido nativo sigue.
    -- No se suma al caso nativo (le pondria el binding del piloto). Lee solo
    -- recovery_cases: esta funcion es security invoker.
    if exists (
        select 1
          from public.recovery_cases native_case
         where native_case.contact_id = p_contact_id
           and native_case.source = v_event.source
           and native_case.external_product_id = p_external_product_id
           and native_case.status in ('grace_period', 'active', 'paused')
           and native_case.policy_key like 'flujo:%'
           and native_case.context ->> 'flow_engine_trigger' = 'carrito'
    ) then
        raise exception using errcode = '55000', message = 'pilot_scope_rejected',
            detail = 'flujo_nativo_en_curso';
    end if;
    -- flow_engine_native_legacy_double_case: end

    select rc.id, rc.status, rc.policy_key, rc.policy_version
      into v_case_id, v_case_status, v_case_policy_key, v_case_policy_version
$replacement$,
    'flow_engine_native_legacy_double_case');

commit;
