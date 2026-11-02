-- followup-stops-after-last-touch-and-pending-payment-v1: dos barreras nuevas
-- en la reserva del seguimiento con cupon, en el legado y en el nativo (E-263,
-- F-149 y F-150; diagnostico: documentacion/2026-11-01-tres-errores-de-
-- mensajes-en-conversaciones-reales.md, errores 2 y 3; decisiones de Dan del
-- 2026-11-01, D2 y D5, con el default recomendado).
--
-- Por que.
-- * F-149 (error 2). Z del carrito (cart_last_touch, att1_ultima_renovacion_01)
--   termina con «Despues de esto no te voy a insistir mas con tu inscripcion».
--   En ATT1 #491 la persona contesto Z, se despidio («sera en otra ocasion») y
--   al dia siguiente le llego el seguimiento con cupon. Lo decidido el
--   2026-10-17 es «sin S despues de Z»: los 30 dias despues de Z bloquean el
--   marketing que nace de nosotros. La reserva del seguimiento no miraba Z.
-- * F-150, D5 (error 3). El seguimiento sale aunque la persona tenga una ficha
--   de pago en efectivo sin vencer (ATT1 #542: la ficha vencia ese dia; Maribel
--   #9 avisaba que pagaba en OXXO). Lo decidido el 2026-10-17: el seguimiento
--   «no sale a quien tiene un pago pendiente».
--
-- Que cambia. Un solo bloque, el mismo texto, en las dos reservas, justo
-- despues de la barrera de la compra (purchase_already_approved). En la
-- portable va antes de la emision del link; en la nativa, antes de la bolsa
-- (flow_engine_native_silence_pool), asi que un freno no deja un link
-- reservado ni gasta un tope. Un rechazo no escribe nada.
--   1. blocked_after_last_touch: a esta persona (el contacto de la
--      conversacion, o la conversacion misma) le salio Z en los ultimos 30
--      dias (D2): una accion del paso cart_last_touch con un intento aceptado
--      por Chatwoot (accepted_by_chatwoot, o resuelto asi en la
--      reconciliacion) en la ventana. La base sabe cuando se acepto: no se lee
--      texto. Solo Z: el seguimiento del pago rechazado (payment_failure_followup)
--      no entra (no es lo pedido).
--   2. blocked_pending_payment: esta persona tiene una ficha vigente (D5):
--      un PURCHASE_BILLET_PRINTED de Hotmart con su telefono (los ultimos 9
--      digitos, como la barrera de la compra) o su mail, recibido en los
--      ultimos 7 dias, sin un PURCHASE_EXPIRED de la misma transaccion
--      despues. Es el evento y no la secuencia de la ficha a proposito: al
--      aceptarse C3, el ultimo recordatorio, la secuencia se cierra
--      (sequence_exhausted) y la ficha sigue vigente ese dia (#542: C3 a las
--      21:00, el seguimiento a las 21:02). Y Maribel guarda la ficha aunque
--      no la recupere. Los 7 dias son un techo: el vencimiento real lo calcula
--      el bridge con el manifiesto (bridge.payment_slip, OXXO de ATT1 a 2 o 3
--      dias) y no se duplica aca; el PURCHASE_EXPIRED lo acorta. Una compra ya
--      aprobada la frena antes la barrera de la compra.
-- Sin Z y sin ficha, las dos dan falso y la reserva da lo de hoy.
--
-- Lo que cambia fuera de la base, en el mismo PR (F-150, D3 y D4):
-- * el bridge suma «lead_declined»: una lista corta de frases de rechazo
--   (bridge/lead_decline.py) escritas despues de nuestra ultima plantilla. El
--   seguimiento con cupon no sale (followup_discount.py) y el recordatorio de
--   la ficha (C2 y C3) cierra sin mensaje con ese motivo (worker.py). La
--   persona no queda dada de baja.
-- * Esto cambia A PROPOSITO lo que dice 20261019000800 en su cabecera («Un
--   "no" escrito que no sea una frase de baja no corta el recordatorio»): con
--   D4, un «no» claro de la lista si lo corta. Contestar sin un «no» claro lo
--   sigue dejando salir, como decidio esa migracion, y la base no cambia: el
--   corte lo hace el bridge, que es el que lee la conversacion.
-- * Los dos resultados nuevos estan en FOLLOWUP_CLAIM_OUTCOMES del bridge.
--
-- Lo que no cambia: claim_conversation_followup_v1 (la compartida, la de
-- Johanna), la admision, la emision del link, la bolsa, settle, los permisos y
-- ninguna tabla. No crea funciones ni roles, no toca filas.
--
-- Como. Cada reserva se redefine desde su definicion VIGENTE
-- (pg_get_functiondef) con un reemplazo contado, el metodo de 20261101000200:
-- el ancla (el final de la barrera de la compra, con la linea que la sigue en
-- cada una) tiene que aparecer exactamente una vez, cada reserva tiene que
-- tener las marcas de 20261101000200 y ninguna la marca de esta. Si algo no
-- coincide, aborta con 55000 y no cambia nada: si alguien cambio la reserva,
-- hay que mirarlo en vez de parchear a ciegas. Correrla dos veces aborta con
-- 55000. create or replace conserva dueno, security definer, search_path y
-- permisos (se verifican al final).
--
-- Errores: 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

create or replace function pg_temp.followup_stops_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text, p_required text[])
returns void
language plpgsql
as $function$
declare
    v_definition text;
    v_required text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'followup_stops_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'followup_stops_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'followup_stops_already_applied',
            detail = p_signature;
    end if;
    foreach v_required in array p_required loop
        if position(v_required in v_definition) = 0 then
            raise exception using errcode = '55000', message = 'unexpected_followup_claim_definition',
                detail = p_signature || ' sin ' || v_required;
        end if;
    end loop;
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor) then
        raise exception using errcode = '55000', message = 'unexpected_followup_claim_definition',
            detail = p_signature || ' ancla';
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'followup_stops_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

do $stops$
declare
    c_portable constant text :=
        'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)';
    c_native constant text :=
        'public.claim_flow_engine_silence_v1(uuid,integer,text,bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)';
    -- El final de la barrera de la compra, igual en las dos (la nativa es una
    -- copia de la portable, 20261101000200).
    c_purchase_end constant text :=
        E'        outcome := ''purchase_already_approved'';\n'
        || E'        return next;\n'
        || E'        return;\n'
        || E'    end if;\n';
    -- Lo que la sigue en cada una: la emision del link en la portable, la
    -- bolsa en la nativa.
    c_portable_next constant text :=
        E'\n    -- El link: la misma emision que usa el agente, anclada en nuestro ultimo\n';
    c_native_next constant text :=
        E'\n    -- flow_engine_native_silence_pool: begin\n';
    c_block constant text := $block$
    -- followup_stops_after_last_touch_and_pending_payment: begin
    -- F-149 (20261101000300, D2): a quien le salio Z del carrito
    -- (cart_last_touch, «no te voy a insistir mas») en los ultimos 30 dias, el
    -- seguimiento no le sale. Por el contacto o por la conversacion.
    if exists (
        select 1
          from public.followup_delivery_attempts last_touch_attempt
          join public.scheduled_actions last_touch_action
            on last_touch_action.id = last_touch_attempt.action_id
          join public.recovery_cases last_touch_case
            on last_touch_case.id = last_touch_action.recovery_case_id
         where last_touch_action.step_key = 'cart_last_touch'
           and (last_touch_case.contact_id = v_conversation.contact_id
                or last_touch_action.conversation_id = v_conversation.id)
           and ((last_touch_attempt.outcome = 'accepted_by_chatwoot'
                 and last_touch_attempt.accepted_at >= v_now - interval '30 days')
                or (last_touch_attempt.reconciliation_resolution = 'accepted_by_chatwoot'
                    and last_touch_attempt.reconciled_at >= v_now - interval '30 days'))
    ) then
        outcome := 'blocked_after_last_touch';
        return next;
        return;
    end if;

    -- F-150 (20261101000300, D5): una ficha de pago en efectivo vigente. El
    -- PURCHASE_BILLET_PRINTED de esta persona (los ultimos 9 digitos del
    -- telefono, como la compra, o el mail) de los ultimos 7 dias, sin un
    -- PURCHASE_EXPIRED de la misma transaccion despues.
    if exists (
        select 1
          from public.webhook_events slip_event
         where slip_event.source = 'hotmart'
           and slip_event.event_type = 'PURCHASE_BILLET_PRINTED'
           and slip_event.received_at >= v_now - interval '7 days'
           and slip_event.received_at <= v_now
           and (right(regexp_replace(coalesce(
                    slip_event.payload #>> '{data,buyer,checkout_phone}',
                    slip_event.payload #>> '{data,buyer,phone}',
                    ''), '[^0-9]', '', 'g'), 9) = v_phone_tail
                or (v_email is not null
                    and lower(btrim(slip_event.payload #>> '{data,buyer,email}')) = v_email))
           and not exists (
               select 1
                 from public.webhook_events expired_event
                where expired_event.source = 'hotmart'
                  and expired_event.event_type = 'PURCHASE_EXPIRED'
                  and expired_event.payload #>> '{data,purchase,transaction}'
                      = slip_event.payload #>> '{data,purchase,transaction}'
                  and expired_event.received_at >= slip_event.received_at
                  and expired_event.received_at <= v_now)
    ) then
        outcome := 'blocked_pending_payment';
        return next;
        return;
    end if;
    -- followup_stops_after_last_touch_and_pending_payment: end
$block$;
begin
    perform pg_temp.followup_stops_patch(
        c_portable,
        c_purchase_end || c_portable_next,
        c_purchase_end || c_block || c_portable_next,
        'followup_stops_after_last_touch_and_pending_payment',
        array['reserve_portable_checkout_issuance_v2',
              'portable_followup_template_reply: end',
              'flow_engine_native_silence_barrier: end',
              'v_phone_tail', 'v_email']);
    perform pg_temp.followup_stops_patch(
        c_native,
        c_purchase_end || c_native_next,
        c_purchase_end || c_block || c_native_next,
        'followup_stops_after_last_touch_and_pending_payment',
        array['reserve_portable_checkout_issuance_v2',
              'flow_engine_native_silence_gate: end',
              'flow_engine_native_silence_authorization',
              'v_phone_tail', 'v_email']);
end;
$stops$;

-- Los permisos que create or replace conserva: las dos, solo service_role.
do $acl$
declare
    v_signature text;
begin
    foreach v_signature in array array[
        'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)',
        'public.claim_flow_engine_silence_v1(uuid,integer,text,bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)'
    ] loop
        if exists (select 1 from pg_roles where rolname = 'service_role'
                      and not has_function_privilege(rolname, v_signature, 'EXECUTE'))
           or exists (select 1 from pg_roles where rolname in ('anon', 'authenticated')
                         and has_function_privilege(rolname, v_signature, 'EXECUTE'))
           or not (select p.prosecdef from pg_proc p where p.oid = v_signature::regprocedure) then
            raise exception using errcode = '55000', message = 'followup_stops_acl_unexpected',
                detail = v_signature;
        end if;
    end loop;
end;
$acl$;

commit;
