-- flow-engine-native-campaign-origin-v1: de que landing y de que oferta viene
-- el recorrido nativo de una accion (E-321, 2026-11-03). Contrato:
-- docs/contracts/campaign-entry-v1.md.
--
-- Por que. Dan, el 03/11: el carrito de una campana se identifica por la
-- landing que vio la persona. Las landings de Black November de ATT1 son otras
-- que las de siempre, asi que el formulario llega desde otra landing, y la
-- intencion que la admision portable correlaciono con el carrito ya la trae
-- (purchase_intents.landing_ref). Medido en ATT1 (04/10-03/11): el 87,4 % de
-- los carritos (624 de 714) llegan cubiertos por una intencion del formulario
-- en las 24 h previas. Con esto el bridge le pone a la conversacion la
-- etiqueta de la campana antes del primer envio del carrito, para que cuando
-- la persona conteste el agente ya tenga el conocimiento de la campana
-- (segment-labels-v1, E-316). La oferta del evento queda como respaldo: si la
-- campana declara una oferta propia, tambien la identifica.
--
-- Que suma. Una sola funcion de lectura, flow_engine_native_campaign_origin_v1,
-- solo para service_role. Para la accion de un recorrido nativo devuelve:
--   * disparador: el trigger_event del recorrido (compra, carrito,
--     pago_fallido, intencion);
--   * oferta: la oferta del evento (flow_engine_native_runs.offer_code);
--   * landing: la landing_ref de la intencion del recorrido
--     (audience_purchase_intent_id, la que la admision portable correlaciono
--     con ESTE evento y que da el permiso del carrito, 20261028000200). Solo si
--     la intencion es real (provider_observed): una intencion fabricada por la
--     reserva de un link no dice donde estuvo la persona. Sin intencion, nulo.
-- Para cualquier otra accion (no nativa, o que no existe) devuelve nulo.
-- No devuelve mail, telefono, nombre ni payload.
--
-- Solo lectura: no cambia ninguna tabla, funcion, permiso ni fila. Correrla dos
-- veces aborta con 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.flow_engine_native_runs') is null
       or to_regclass('public.purchase_intents') is null
       or to_regclass('public.scheduled_actions') is null
       or not exists (
           select 1 from information_schema.columns
            where table_schema = 'public' and table_name = 'flow_engine_native_runs'
              and column_name = 'audience_purchase_intent_id'
       ) then
        raise exception using errcode = '55000', message = 'flow_engine_native_campaign_origin_prerequisite_missing';
    end if;
    if to_regprocedure('public.flow_engine_native_campaign_origin_v1(uuid)') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_native_campaign_origin_already_applied';
    end if;
end;
$already$;

create function public.flow_engine_native_campaign_origin_v1(p_action_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    -- flow_engine_native_campaign_origin: la landing y la oferta del recorrido.
    select jsonb_build_object(
               'disparador', native.trigger_event,
               'oferta', native.offer_code,
               'landing', case when intent.provider_observed then intent.landing_ref end)
      from public.scheduled_actions action_row
      join public.flow_engine_native_runs native on native.recovery_case_id = action_row.recovery_case_id
      left join public.purchase_intents intent on intent.id = native.audience_purchase_intent_id
     where action_row.id = p_action_id
$function$;

do $acl$
declare
    v_signature text := 'public.flow_engine_native_campaign_origin_v1(uuid)';
    v_role text;
begin
    execute format('revoke all on function %s from public', v_signature);
    foreach v_role in array array['anon', 'authenticated'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function %s from %I', v_signature, v_role);
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        execute format('grant execute on function %s to service_role', v_signature);
    end if;
    if exists (
           select 1 from pg_roles
            where rolname = 'service_role'
              and not has_function_privilege(rolname, v_signature, 'EXECUTE')
       )
       or exists (
           select 1 from pg_roles
            where rolname in ('anon', 'authenticated')
              and has_function_privilege(rolname, v_signature, 'EXECUTE')
       ) then
        raise exception using errcode = '55000',
            message = 'flow_engine_native_campaign_origin_acl_unexpected',
            detail = format('solo service_role ejecuta %s', v_signature);
    end if;
end;
$acl$;

commit;
