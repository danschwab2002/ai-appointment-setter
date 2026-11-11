-- pending-cancel-keeps-first-contact-v1: un pago pendiente que se cancela
-- (Mercado Pago, Nequi, transferencia) ya no clasifica la intencion como pago
-- rechazado (E-423, informe E-414 del programa etapa-1, decision de Dan del
-- 2026-11-10).
--
-- Por que. Hotmart manda PURCHASE_CANCELED tanto para un rechazo de la tarjeta
-- como para un pago pendiente que vencio sin pagarse (Mercado Pago lo cancela a
-- los ~134 minutos). El correlador portable
-- (_correlate_portable_hotmart_purchase_intent, derivado en 20261001000100 y
-- parcheado en 20261019000100) clasifica la intencion como
-- payment_failure_supported en los dos casos. Esa clasificacion frena el primer
-- contacto del formulario (_portable_precheckout_stop_reason:
-- superseded_by_provider_event, al planificar y al reevaluar) y la adopcion de
-- la respuesta (20261009000200 y la nativa de 20261101000100). Pero el pago
-- rechazado no planifica nada para un pendiente: 20261019000200 lo descarta con
-- payment_failure_not_a_refusal. Resultado: quien elige Mercado Pago, no paga y
-- habia dejado el formulario, se queda sin el primer contacto que ya estaba
-- planificado. Medido el 2026-11-10 en solo lectura: en ATT1, 1 persona
-- (dos primeros contactos perdidos, el 03/11 y el 06/11); en Maribel, ninguna
-- (sus 7 pendientes cancelados no cruzaron con una intencion).
--
-- Que cambia. Una sola rama del correlador portable, por pg_get_functiondef +
-- replace con la cantidad exacta de ocurrencias (el metodo de 20261019000100):
-- un PURCHASE_CANCELED resuelto cuya clase es not_a_refusal
-- (payment_failure_refusal_kind, la misma regla de 20261019000200: sin motivo y
-- con un medio que no es tarjeta ni PayPal) deja la clasificacion de la
-- intencion como estaba, igual que la ficha impresa. La correlacion se sigue
-- guardando resuelta, con su evento.
--
-- Por que en el correlador y no en los frenos. Es una sola rama, y todos los
-- que leen la clasificacion quedan de acuerdo a la vez: el freno del primer
-- contacto al planificar y al reevaluar, la adopcion de la respuesta (legado y
-- nativo) y el simulador de paridad del bridge (flow_parity.py). Corregir los
-- frenos eran cuatro parches en tres funciones derivadas, y la intencion
-- seguiria diciendo "pago rechazado" para un pago que nunca fue rechazado.
--
-- Lo que NO cambia:
-- - Un rechazo de verdad (tarjeta o PayPal, o con motivo) clasifica igual.
-- - Una intencion ya clasificada (un rechazo o un carrito antes) no se toca:
--   el pendiente cancelado despues no la desclasifica.
-- - El pago rechazado (legado y nativo) sigue descartando el pendiente con su
--   propio motivo: decide con payment_failure_refusal_kind y no lee la
--   clasificacion.
-- - El correlador de Johanna (correlate_hotmart_purchase_intent): no se toca.
-- - Las intenciones ya clasificadas en produccion: no se reescriben.
--
-- Errores: 55000 si la definicion vigente del correlador no es la esperada
-- (otra migracion la cambio) o si se corre dos veces.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $pending_cancel$
declare
    v_definition text;
    v_old constant text := $old$        elsif v_event_type = 'PURCHASE_CANCELED' then
            update public.purchase_intents
            set current_classification = 'payment_failure_supported',$old$;
    v_new constant text := $new$        elsif v_event_type = 'PURCHASE_CANCELED'
              and public.payment_failure_refusal_kind(
                  v_event.payload #>> '{data,purchase,payment,refusal_reason}',
                  v_event.payload #>> '{data,purchase,payment,type}'
              ) = 'not_a_refusal' then
            -- pending_cancel_keeps_first_contact: begin
            -- Un pago pendiente que se cancelo no es un rechazo: la intencion
            -- sigue esperando la compra y su clasificacion no cambia (E-423).
            perform 1
            from public.purchase_intents
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
            -- pending_cancel_keeps_first_contact: end
        elsif v_event_type = 'PURCHASE_CANCELED' then
            update public.purchase_intents
            set current_classification = 'payment_failure_supported',$new$;
begin
    if to_regprocedure('public.payment_failure_refusal_kind(text,text)') is null then
        raise exception using errcode = '55000',
            message = 'pending_cancel_keeps_first_contact_prerequisite_missing';
    end if;
    select pg_get_functiondef(
        'public._correlate_portable_hotmart_purchase_intent(uuid)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or position('pending_cancel_keeps_first_contact' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'pending_cancel_keeps_first_contact_already_applied';
    end if;
    if length(v_definition) - length(replace(v_definition, v_old, ''))
          <> length(v_old)
       or position('v_event.payload' in v_definition) = 0
       or position('v_resolved_intent_id' in v_definition) = 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_portable_hotmart_intent_correlator_definition';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$pending_cancel$;

commit;
