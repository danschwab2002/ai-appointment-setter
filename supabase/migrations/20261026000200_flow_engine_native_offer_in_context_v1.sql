-- Migration: el caso de un flujo nativo lleva al despacho la oferta del evento
-- que lo abrio (E-122, 2026-10-26).
--
-- Por que. La compra de prueba del 2026-10-26 16:47:37Z en ATT1 abrio el
-- primer recorrido nativo (att1/bienvenida) y la plantilla nunca salio: 4
-- intentos failed_before_request y la accion en permanent_failed, con
-- followup_recipient_not_allowlisted en el log del bridge. El despachador
-- valida al destinatario con _is_authorized_followup_recipient
-- (src/bridge/worker.py): sin JID fijo exige que execution_context.offer_code
-- este entre las ofertas aceptadas del binding. get_followup_execution_context
-- (20260803000100) devuelve rc.offer_code, y _flow_engine_admit_native_flow
-- (20261024000200) crea el caso con offer_code nulo. Con eso, ningun paso de
-- mensaje de un flujo nativo podia salir en ninguna instancia. El evento
-- capturado esta en tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json.
--
-- Por que no se llena recovery_cases.offer_code. El nulo es a proposito (lo
-- dice la admision nativa): los planificadores buscan el caso abierto de la
-- misma persona, producto y oferta por esa columna (el carrito del motor,
-- 20261022000100; el pago rechazado, 20261019000200; la compra,
-- 20260808000100), y un carrito o un pago nunca tienen que caer en un caso
-- nativo. Llenar la columna reabria eso.
--
-- Que cambia. Dos reemplazos contados sobre la definicion viva:
-- 1. _flow_engine_admit_native_flow guarda la oferta del evento (la misma que
--    escribe en flow_engine_native_runs) en recovery_cases.context, como
--    flow_engine_offer. offer_code sigue nulo.
-- 2. get_followup_execution_context devuelve, para un caso nativo (policy_key
--    flujo:...) sin offer_code, esa oferta del context. Para todo otro caso
--    devuelve rc.offer_code como hasta hoy.
-- El chequeo del bridge no cambia: la oferta del evento tiene que ser una de
-- las aceptadas del binding y el producto el del binding. Una compra por una
-- oferta que la instancia no declara, o sin oferta, se sigue frenando.
--
-- No se lee flow_engine_native_runs desde get_followup_execution_context: es
-- security invoker y esa tabla no tiene grants para service_role
-- (20261024000200). recovery_cases si los tiene.
--
-- Los casos nativos que ya existen no se tocan: su context no tiene la oferta
-- y su envio se sigue frenando (fail closed).
--
-- Grants: create or replace (lo que hace execute de la definicion) conserva los
-- privilegios de la funcion, asi que el ACL no cambia.

begin;

do $flow_engine_native_offer_in_context$
declare
    v_admit constant text :=
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)';
    v_admit_old constant text :=
        E'        jsonb_build_object(''flow_engine_event_id'', p_event.id, ''flow_engine_trigger'',\n'
        || E'                           public._flow_engine_native_trigger(v_definition.definition)))\n'
        || E'    returning id into v_case_id;\n';
    v_admit_new constant text :=
        E'        -- flow_engine_native_offer_in_context: la oferta del evento va al\n'
        || E'        -- context (20261026000200): get_followup_execution_context la lee para\n'
        || E'        -- el chequeo del destinatario del despachador.\n'
        || E'        jsonb_build_object(''flow_engine_event_id'', p_event.id, ''flow_engine_trigger'',\n'
        || E'                           public._flow_engine_native_trigger(v_definition.definition),\n'
        || E'                           ''flow_engine_offer'', p_offer))\n'
        || E'    returning id into v_case_id;\n';
    v_context constant text :=
        'public.get_followup_execution_context(uuid,text,bigint,timestamptz)';
    v_context_old constant text :=
        E'        rc.offer_code,\n'
        || E'        rc.current_goal,\n';
    v_context_new constant text :=
        E'        -- flow_engine_native_offer_in_context: un caso nativo nace sin\n'
        || E'        -- offer_code (20261024000200) y trae la oferta de su evento en el\n'
        || E'        -- context (20261026000200).\n'
        || E'        case when rc.offer_code is null and rc.policy_key like ''flujo:%''\n'
        || E'             then rc.context ->> ''flow_engine_offer''\n'
        || E'             else rc.offer_code end,\n'
        || E'        rc.current_goal,\n';
    v_definition text;
begin
    select pg_get_functiondef(v_admit::regprocedure) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_admit_old, '')) <> length(v_admit_old)
       or position('flow_engine_native_offer_in_context' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_flow_engine_native_admission_definition', detail = v_admit;
    end if;
    execute replace(v_definition, v_admit_old, v_admit_new);

    select pg_get_functiondef(v_context::regprocedure) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_context_old, '')) <> length(v_context_old)
       or position('flow_engine_native_offer_in_context' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_followup_execution_context_definition', detail = v_context;
    end if;
    execute replace(v_definition, v_context_old, v_context_new);

    if (select length(definition) - length(replace(definition, v_admit_new, ''))
          from (select pg_get_functiondef(v_admit::regprocedure) as definition) live) <> length(v_admit_new)
       or (select length(definition) - length(replace(definition, v_context_new, ''))
          from (select pg_get_functiondef(v_context::regprocedure) as definition) live) <> length(v_context_new) then
        raise exception using errcode = '55000', message = 'flow_engine_native_offer_in_context_not_applied';
    end if;
end;
$flow_engine_native_offer_in_context$;

commit;
